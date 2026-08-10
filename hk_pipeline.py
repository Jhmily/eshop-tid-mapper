#!/usr/bin/env python3
"""HK 区 NSUID -> TID 映射工具
用法: python hk_pipeline.py
产出: output/hk_tid_map.json  output/hk_tid_map_unmatched.json
标准库 only，无外部依赖。
"""

import json
import os
import re
import time
import uuid
import urllib.request
import urllib.error
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

# ============================================================
# 常量
# ============================================================
HK_API = 'https://www.nintendo.com/hk/api/search'
TITLEDB_URL = 'https://raw.githubusercontent.com/blawar/titledb/refs/heads/master/HK.zh.json'
EC_URL = 'https://ec.nintendo.com/HK/zh/titles/{}'
MAX_SIZE = 10_000
EC_THREADS = 1
EC_TIMEOUT = 15
EC_RETRIES = 3
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'

DIR = Path(__file__).parent
CACHE = DIR / '.cache'
OUT = DIR / 'output'

# ============================================================
# 代理 (Windows 注册表)
# ============================================================
def _get_proxy() -> str | None:
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r'Software\Microsoft\Windows\CurrentVersion\Internet Settings')
        enabled, _ = winreg.QueryValueEx(key, 'ProxyEnable')
        if enabled:
            server, _ = winreg.QueryValueEx(key, 'ProxyServer')
            winreg.CloseKey(key)
            if server:
                server = server.replace(' ', '')
                if '=' in server:
                    for part in server.split(';'):
                        if '=' in part:
                            _, val = part.split('=', 1)
                            if val:
                                return f'http://{val}'
                return f'http://{server}'
        winreg.CloseKey(key)
    except Exception:
        pass
    return None


def _install_proxy() -> str | None:
    proxy = _get_proxy()
    if proxy:
        handler = urllib.request.ProxyHandler({'http': proxy, 'https': proxy})
        urllib.request.install_opener(urllib.request.build_opener(handler))
    return proxy

# ============================================================
# HTTP
# ============================================================
def fetch(url: str, retries: int = 3, timeout: int = 30,
          headers: dict | None = None) -> tuple[bytes | None, int, dict]:
    """-> (body, status_code, response_headers).
    body=None on failure. 304 returns (None, 304, headers)."""
    hdrs = {'User-Agent': UA}
    if headers:
        hdrs.update(headers)
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read(), resp.status, dict(resp.headers)
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return None, 304, dict(e.headers)
            if e.code == 404:
                return None, 404, dict(e.headers)
            last_err = e
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            last_err = e
        if attempt < retries:
            time.sleep(attempt * 0.5)
    print(f'  fetch fail ({retries}x): {url} - {last_err}')
    return None, 0, {}

# ============================================================
# ETag 增量下载
# ============================================================
def download_with_etag(url: str, dest: Path) -> bool:
    """下载到 dest，支持 ETag 304。返回 True=数据已就绪可用。"""
    etag_file = dest.parent / f'{dest.name}.etag'
    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.exists() and etag_file.exists():
        etag = etag_file.read_text().strip()
        _, status, _ = fetch(url, headers={'If-None-Match': etag})
        if status == 304:
            dest.touch()
            return True

    print(f'  下载 {dest.name} ...', end='', flush=True)
    body, _, resp_headers = fetch(url, timeout=120)
    if body is None:
        print(' 失败')
        return dest.exists()

    # 原子写入
    tmp = dest.parent / f'{dest.name}.{uuid.uuid4().hex[:8]}.tmp'
    tmp.write_bytes(body)
    tmp.replace(dest)

    new_etag = resp_headers.get('ETag') or resp_headers.get('etag')
    if new_etag:
        etag_file.write_text(new_etag)
    elif etag_file.exists():
        etag_file.unlink()

    size_mb = dest.stat().st_size / (1024 * 1024)
    print(f' OK ({size_mb:.0f}MB)')
    return True

# ============================================================
# HK Contentful API
# ============================================================
def _pull_page(p: int, size: int) -> list[dict]:
    url = f'{HK_API}?k=switch&directory=software&size={size}&p={p}'
    body, _, _ = fetch(url, timeout=60)
    if body is None:
        return []
    try:
        return json.loads(body).get('items', [])
    except json.JSONDecodeError:
        return []


def pull_hk_api() -> list[dict]:
    """返回全量 items 列表（去重后）。"""
    body, _, _ = fetch(f'{HK_API}?k=switch&directory=software&size=1&p=1',
                       timeout=30)
    total = 0
    if body:
        try:
            total = json.loads(body).get('total', 0)
        except json.JSONDecodeError:
            pass

    print(f'  HK API total={total}', end='', flush=True)

    if total and total <= MAX_SIZE:
        items = _pull_page(1, total)
        print(f' 单页 {len(items)}条')
    elif total and total > MAX_SIZE:
        pages = (total + MAX_SIZE - 1) // MAX_SIZE
        print(f' 分页 {pages}页')
        items = []
        with ThreadPoolExecutor(max_workers=min(pages, 3)) as ex:
            futures = {ex.submit(_pull_page, p, MAX_SIZE): p
                       for p in range(1, pages + 1)}
            for f in as_completed(futures):
                items.extend(f.result())
    else:
        # 嗅探失败，逐页探测
        print(' 探测模式')
        items = []
        for p in range(1, 4):
            page_items = _pull_page(p, MAX_SIZE)
            items.extend(page_items)
            if len(page_items) < MAX_SIZE:
                break

    # 去重 + 排除 NS2 Edition (同TID变体)
    seen: set[str] = set()
    deduped = []
    for it in items:
        nsuid = it.get('nsuid', '')
        if (nsuid and nsuid not in seen
                and it.get('hardwareCategory') != 'Nintendo Switch 2 Edition'):
            seen.add(nsuid)
            deduped.append(it)
    return deduped

# ============================================================
# titledb
# ============================================================
def load_titledb(path: Path) -> dict[str, str]:
    """{nsuid: tid} — 仅 BASE (id 末3位=000)。"""
    print(f'  解析 {path.name} ...', end='', flush=True)
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    result = {}
    for nsuid_str, entry in data.items():
        tid = entry.get('id', '')
        if isinstance(tid, str) and len(tid) == 16 and tid[-3:] == '000':
            name = entry.get('name', '') or ''
            if 'Nintendo Switch 2 Edition' in name:
                continue
            result[nsuid_str] = tid.upper()
    print(f' {len(result)} BASE')
    return result

# ============================================================
# TID 提取 (ec.nintendo 页面 HTML)
# ============================================================
# Phase 1: RSC payload \"applications\":[{\"id\":\"0100...\"}]
_APP_RE = re.compile(
    r'\\"applications\\":\[{\\"id\\":\\"((?:0100|0400)[a-fA-F0-9]{12})\\"'
)
# Phase 2: 全页扫描 0100*/0400*, 优先末3位=000
_TID_RE = re.compile(r'\b(?:0100|0400)[a-fA-F0-9]{12}\b')


def extract_tid(html: str) -> str | None:
    m = _APP_RE.search(html)
    if m:
        return m.group(1).upper()
    tids = _TID_RE.findall(html)
    if not tids:
        return None
    base = [t for t in tids if t[-3:] == '000']
    return (base[0] if base else tids[0]).upper()

# ============================================================
# ec.nintendo 补漏
# ============================================================
def _scrape_one(nsuid: str) -> tuple[str | None, bool]:
    """-> (tid_or_None, is_404)."""
    for attempt in range(EC_RETRIES):
        try:
            body, status, _ = fetch(EC_URL.format(nsuid), retries=1,
                                    timeout=EC_TIMEOUT)
            if status == 404:
                return None, True
            if body is None or status >= 400:
                if attempt < EC_RETRIES - 1:
                    time.sleep(attempt * 0.5)
                continue
            tid = extract_tid(body.decode('utf-8', errors='replace'))
            if tid:
                return tid, False
            return None, False  # 页面有但无 TID (未发售)
        except Exception:
            if attempt < EC_RETRIES - 1:
                time.sleep(attempt * 0.5)
    return None, False


def scrape_batch(nsuids: list[str]) -> tuple[dict[str, str], set[str]]:
    """并发爬 ec.nintendo。-> ({nsuid: tid}, not_found_nsuids)"""
    total = len(nsuids)
    if not total:
        return {}, set()
    result: dict[str, str] = {}
    not_found: set[str] = set()
    done = 0
    with ThreadPoolExecutor(max_workers=EC_THREADS) as ex:
        futures = {ex.submit(_scrape_one, n): n for n in nsuids}
        for f in as_completed(futures):
            nsuid = futures[f]
            tid, is_404 = f.result()
            if tid:
                result[nsuid] = tid
            elif is_404:
                not_found.add(nsuid)
            done += 1
            print(f'\r  ec.nintendo {done}/{total}', end='', flush=True)
    print()
    return result, not_found

# ============================================================
# 状态管理
# ============================================================
def now_iso() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def load_state() -> dict:
    state_file = CACHE / '.hk_state.json'
    if state_file.exists():
        try:
            return json.loads(state_file.read_text('utf-8'))
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state: dict):
    OUT.mkdir(parents=True, exist_ok=True)
    (CACHE / '.hk_state.json').write_text(
        json.dumps(state, ensure_ascii=False, indent=2), 'utf-8')


def load_output_map() -> dict[str, list]:
    map_file = OUT / 'hk_tid_map.json'
    if map_file.exists():
        try:
            return json.loads(map_file.read_text('utf-8'))
        except json.JSONDecodeError:
            return {}
    return {}

# ============================================================
# 合并 (核心逻辑)
# ============================================================
def merge(api_items: list[dict], titledb: dict[str, str],
          state: dict, existing_map: dict[str, list]
          ) -> tuple[dict[str, str], list[str], list[dict]]:
    """
    -> (matched_nsuid_tid, to_scrape_nsuids, unmatched_bundles)
    """
    matched: dict[str, str] = {}
    to_scrape: list[str] = []
    unmatched: list[dict] = []

    # 同步: existing_map 里有但 state 里没有的 → 用户手动加的，标记 manual
    for nsuid in existing_map:
        if nsuid not in state:
            state[nsuid] = {'source': 'manual',                             'tid': existing_map[nsuid][0]}
        elif 'tid' not in state[nsuid]:
            # 回填: 旧 state 没有 tid 字段，从 existing_map 补上
            state[nsuid]['tid'] = existing_map[nsuid][0]

    for item in api_items:
        nsuid = item['nsuid']
        prefix = nsuid[:6]

        if prefix != '700100':
            unmatched.append({
                'nsuid': nsuid,
                'title': item.get('title', ''),
                'reason': 'bundle',
                'last_checked': now_iso()
            })
            continue

        rec = state.get(nsuid, {})
        src = rec.get('source', '')

        # 下架 / bundle → 永远跳过
        if src in ('not_found', 'bundle'):
            continue

        # titledb 来源: 已有 map 或用 state 中 tid / titledb 恢复
        if src == 'titledb':
            if nsuid in existing_map:
                matched[nsuid] = existing_map[nsuid][0]
            elif rec.get('tid'):
                matched[nsuid] = rec['tid']
            elif nsuid in titledb:
                matched[nsuid] = titledb[nsuid]
            else:
                to_scrape.append(nsuid)
            continue

        # ec_nintendo 来源: 已有 map 或用 state 中 tid 恢复，丢了才重爬
        if src == 'ec_nintendo':
            if nsuid in existing_map:
                matched[nsuid] = existing_map[nsuid][0]
            elif rec.get('tid'):
                matched[nsuid] = rec['tid']
            else:
                to_scrape.append(nsuid)
            continue

        # manual → 重查 (官方可能已补上)
        if src == 'manual':
            if nsuid in titledb:
                state[nsuid] = {'source': 'titledb',                                 'tid': titledb[nsuid]}
                matched[nsuid] = titledb[nsuid]
                continue
            if nsuid in existing_map:
                to_scrape.append(nsuid)
            else:
                to_scrape.append(nsuid)
            continue

        # 全新条目
        if nsuid in titledb:
            state[nsuid] = {'source': 'titledb',                             'tid': titledb[nsuid]}
            matched[nsuid] = titledb[nsuid]
        else:
            to_scrape.append(nsuid)

    # API 没返回的已有条目保底 + titledb 独有条目入库(排除NS2E)
    for nsuid in existing_map:
        if nsuid not in matched:
            matched[nsuid] = existing_map[nsuid][0]
    for nsuid, tid in titledb.items():
        if nsuid.startswith('7001') and nsuid not in matched:
            matched[nsuid] = tid

    return matched, to_scrape, unmatched

# ============================================================
# 输出
# ============================================================
def build_and_save_output(api_items: list[dict], matched: dict[str, str],
                          state: dict, to_scrape: list[str],
                          scraped: dict[str, str], not_found: set[str],
                          unmatched: list[dict]):
    OUT.mkdir(parents=True, exist_ok=True)
    existing_map = load_output_map()

    # 构建 {nsuid: {release_date, name}}
    api_index: dict[str, dict] = {}
    for item in api_items:
        nsuid = item['nsuid']
        if nsuid[:6] != '700100':
            continue
        rd = item.get('releaseDate', '')
        langs = item.get('supportedLanguages', [])
        api_index[nsuid] = {
            'release_date': rd[:10] if rd and rd.startswith('20') else '',
            'name': item.get('title', ''),
            'languages': ', '.join(langs) if isinstance(langs, list) else '',
        }

    # 合并所有 TID
    all_tids: dict[str, str] = dict(matched)
    all_tids.update(scraped)

    # 更新 state
    for nsuid in scraped:
        state[nsuid] = {'source': 'ec_nintendo',                         'tid': scraped[nsuid]}
    for nsuid in not_found:
        state[nsuid] = {'source': 'not_found'}
    for nsuid in to_scrape:
        if nsuid not in scraped and nsuid not in not_found:
            if state.get(nsuid, {}).get('source') == 'manual':
                if nsuid in existing_map:
                    all_tids[nsuid] = existing_map[nsuid][0]
                    state[nsuid]['tid'] = existing_map[nsuid][0]
                continue
            state[nsuid] = {'source': 'no_tid_yet'}

    # 从 state + 当前 scrape 重建 unmatched
    for nsuid, rec in state.items():
        src = rec.get('source', '')
        if src in ('bundle', 'not_found', 'no_tid_yet'):
            if nsuid in all_tids:
                continue
            item = api_index.get(nsuid, {})
            unmatched.append({
                'nsuid': nsuid,
                'title': item.get('name', ''),
                'reason': src,
                'last_checked': rec.get('last_checked', '')
            })

    # 当前 scrape 中新发现的 not_found
    for nsuid in not_found:
        if nsuid not in state:
            item = api_index.get(nsuid, {})
            unmatched.append({
                'nsuid': nsuid,
                'title': item.get('name', ''),
                'reason': 'not_found',
                'last_checked': now_iso()
            })

    # 排序: 发布日期 → NSUID
    def _sort_key(nsuid_tid):
        nsuid, _ = nsuid_tid
        rd = api_index.get(nsuid, {}).get('release_date', '')
        return (rd or '9999-99-99', int(nsuid))

    # titledb 元数据补充: API 没返回的条目从 titledb 拿
    tdb_meta = {}
    tdb_path = CACHE / 'HK.zh.json'
    if tdb_path.exists():
        with open(tdb_path, encoding='utf-8') as f:
            for k, v in json.load(f).items():
                tid = v.get('id', '')
                if not isinstance(tid, str) or len(tid) != 16 or tid[-3:] != '000':
                    continue
                tdb_meta[k] = {
                    'name': v.get('name', ''),
                    'release_date': str(v.get('releaseDate', '')),
                    'languages': ', '.join(v.get('languages', []))
                }

    result = {}
    for nsuid, tid in sorted(all_tids.items(), key=_sort_key):
        info = api_index.get(nsuid) or tdb_meta.get(nsuid, {})
        result[nsuid] = [tid, info.get('name', ''),
                         info.get('release_date', ''), info.get('languages', '')]

    (OUT / 'hk_tid_map.json').write_text(
        json.dumps(result, ensure_ascii=False, indent=2), 'utf-8')
    print(f'  hk_tid_map.json: {len(result)} 条')

    unmatched.sort(key=lambda x: int(x['nsuid']))
    (OUT / 'hk_tid_map_unmatched.json').write_text(
        json.dumps(unmatched, ensure_ascii=False, indent=2), 'utf-8')
    print(f'  hk_tid_map_unmatched.json: {len(unmatched)} 条')

    save_state(state)

# ============================================================
# main
# ============================================================
def main():
    t0 = time.time()

    for d in [CACHE, OUT]:
        d.mkdir(parents=True, exist_ok=True)

    _install_proxy()
    print(f'=== HK 区 NSUID -> TID 映射 ===')

    # 1+2: HK API + titledb 并行
    print(f'\n[1+2] HK API | titledb 并行拉取 ...')
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_api = ex.submit(pull_hk_api)
        f_db = ex.submit(lambda: (
            load_titledb(CACHE / 'HK.zh.json')
            if download_with_etag(TITLEDB_URL, CACHE / 'HK.zh.json')
            else {}
        ))

    api_items = f_api.result()
    (CACHE / 'hk_api.json').write_text(
        json.dumps(api_items, ensure_ascii=False, indent=2), 'utf-8')
    print(f'  HK API: {len(api_items)} 条')

    titledb = f_db.result()
    print(f'  titledb: {len(titledb)} BASE')

    # 3. 合并
    print(f'\n[3] 合并 + 增量 ...')
    state = load_state()
    existing_map = load_output_map()
    prev_total = len(existing_map)
    matched, to_scrape, unmatched = merge(api_items, titledb, state, existing_map)
    manual_count = sum(1 for v in state.values() if v.get('source') == 'manual')
    new_matched = sum(1 for nsuid in matched if nsuid not in existing_map)
    print(f'  matched: {len(matched)} (+{new_matched}新)  '
          f'to_scrape: {len(to_scrape)}  unmatched: {len(unmatched)}  '
          f'manual: {manual_count}')
    if prev_total:
        print(f'  上次 TID 总量: {prev_total}, 本次 API: {len(api_items)} 条')

    # 4. 补漏
    if to_scrape:
        ec_retry = sum(1 for n in to_scrape
                       if state.get(n, {}).get('source') in ('manual', 'no_tid_yet'))
        ec_new = len(to_scrape) - ec_retry
        parts = [f'{len(to_scrape)} 条']
        if ec_new:
            parts.append(f'{ec_new}新')
        if ec_retry:
            parts.append(f'{ec_retry}重试')
        label = ': '.join(parts) if len(parts) > 1 else parts[0]
        print(f'\n[4] ec.nintendo 补漏 ({label}, {EC_THREADS}线程) ...')
        scraped, not_found = scrape_batch(to_scrape)
        newly_got = sum(1 for n in scraped if n not in existing_map)
        print(f'  拿到 TID: {len(scraped)} (新增 +{newly_got})  404: {len(not_found)}')
    else:
        print(f'\n[4] 无需补漏')
        scraped, not_found = {}, set()

    # 输出
    print(f'\n[=] 输出 ...')
    build_and_save_output(api_items, matched, state, to_scrape,
                          scraped, not_found, unmatched)

    elapsed = time.time() - t0
    final_total = len(matched) + len(scraped)
    print(f'\n完成 ({elapsed:.0f}s)')
    if prev_total:
        new_tids = final_total - prev_total
        print(f'  TID: {prev_total} → {final_total} ({new_tids:+d})')
    if not os.environ.get('CI') and not os.environ.get('GITHUB_ACTIONS'):
        input('\n按回车退出...')


if __name__ == '__main__':
    main()
