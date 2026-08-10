import argparse
import json
import time
from pathlib import Path

DIR = Path(__file__).parent
CACHE = DIR / '.cache'
OUT = DIR / 'output'

from shared.http import fetch, setup_proxy
from shared.tid import derive_parent_tid, extract_base_page
from titledb import download, process
from contentful import pull
from merge import run as merge_run
from dlc import run as dlc_run
from output import write


def main(skip_price: bool = False):
    t0 = time.time()
    setup_proxy()
    for d in (CACHE, OUT):
        d.mkdir(parents=True, exist_ok=True)

    # ---- Step 1 ----
    print('[1] titledb ...')
    titledb_path = CACHE / 'HK.zh.json'
    if not download(titledb_path):
        print('  titledb 下载失败')
        return
    base, dlc, dlc_nsuids = process(titledb_path)
    print(f'  BASE={len(base)}  DLC={len(dlc)}')

    # ---- Step 2 ----
    print('[2] HK API ...')
    api_base = pull(CACHE / 'hk_api.json')
    print(f'  API_BASE={len(api_base)}')

    # ---- Step 3+4A ----
    print('[3+4A] 合并 + ec 补漏 ...')
    matched, unmatched = merge_run(base, api_base, OUT / 'hk_tid_map.json',
                                   CACHE / '.hk_404.json')
    print(f'  matched={len(matched)}  unmatched={len(unmatched)}')

    # ---- Step 4B ----
    print('[4B] DLC 枚举 ...')
    new_dlc, dlc_meta = dlc_run(CACHE / 'enum_dlc_pool.txt', dlc_nsuids, CACHE,
                                CACHE / 'raw_full.json', skip_price=skip_price)
    print(f'  新DLC={len(new_dlc)}  元数据={len(dlc_meta)}')

    # ---- 可疑条目 ec.nintendo 实证验证 ----
    _verify_suspects(base, matched, unmatched)

    # ---- Step 5A ----
    print('[5A] 输出 BASE ...')
    write(matched, unmatched, OUT)
    print(f'  hk_tid_map.json: {len(matched)} 条')

    # ---- Step 5B ----
    print('[5B] DLC 组装 ...')
    _assemble_full(matched, dlc, dlc_meta, OUT)
    print(f'  hk_full.json: {len(matched)} BASE + DLC')

    print(f'完成 ({time.time() - t0:.0f}s)')


def _assemble_full(matched: dict, tdb_dlc: dict, dlc_meta: dict, out_dir: Path):
    # TID → [NSUIDs] 反向索引
    tid_to_nsuids: dict[str, list[str]] = {}
    for nsuid, entry in matched.items():
        tid_to_nsuids.setdefault(entry[0], []).append(nsuid)

    dlc_by_parent: dict[str, list] = {}

    # titledb DLC: TID 推导父
    for nsuid, info in tdb_dlc.items():
        if info.get('_conflict'):
            continue
        parent_tid = derive_parent_tid(info['tid'])
        if not parent_tid:
            continue
        for p_nsuid in tid_to_nsuids.get(parent_tid, []):
            dlc_by_parent.setdefault(p_nsuid, []).append([
                nsuid, info['tid'], info['name'],
                _norm_date(info.get('release_date', ''))])

    # 爬虫 DLC: parent_nsuid 已知
    for nsuid, info in dlc_meta.items():
        parent = info.get('parent_nsuid', '')
        if not parent:
            continue
        dlc_by_parent.setdefault(parent, []).append([
            nsuid, '暂无', info.get('name', ''),
            _norm_date(info.get('date', ''))])

    # 合并
    result: dict = {}
    for nsuid, entry in matched.items():
        new_entry = list(entry[:4])
        dlcs = dlc_by_parent.get(nsuid, [])
        seen = set()
        unique = []
        for d in dlcs:
            if d[0] not in seen:
                seen.add(d[0])
                unique.append(d)
        unique.sort(key=lambda x: (x[3] or '9999-99-99', int(x[0])))
        if unique:
            new_entry.append({'dlcs': unique})
        result[nsuid] = new_entry

    # 排序（统一日期格式防混合排序错乱）
    def _sort_key(item):
        nsuid, entry = item
        d = (entry[2] or '') if len(entry) > 2 else ''
        d = d.strip()
        if len(d) == 8 and d.isdigit():
            d = f'{d[:4]}-{d[4:6]}-{d[6:8]}'
        return (d or '9999-99-99', int(nsuid))
    sorted_result = dict(sorted(result.items(), key=_sort_key))

    (out_dir / 'hk_full.json').write_text(
        json.dumps(sorted_result, ensure_ascii=False, indent=2), 'utf-8')


def _norm_date(d: str) -> str:
    d = (d or '').strip()
    if len(d) == 8 and d.isdigit():
        return f'{d[:4]}-{d[4:6]}-{d[6:8]}'
    return d[:10] if d else ''


def _verify_suspects(base: dict, matched: dict, unmatched: list):
    """对可疑 BASE 条目爬 ec.nintendo 实证验证。
    可疑 = _conflict（TID碰撞）+ 空日期（titledb数据不全）。
    404 → 从 matched 移除加入 unmatched。
    200 + TID 不同 → 用页面 TID 修正。"""
    suspects: dict[str, dict] = {}
    # TID 碰撞
    for n, e in base.items():
        if e.get('_conflict'):
            suspects[n] = dict(e, reason='conflict')
    # 空日期（已在 matched 中但 titledb 缺日期）
    for n, e in matched.items():
        if n not in suspects and not (e[2] if len(e) > 2 else ''):
            suspects[n] = {'name': e[1], 'tid': e[0], 'reason': 'empty_date'}

    if not suspects:
        return
    print(f'  可疑条目 ec 验证 ({len(suspects)}个) ...')
    fixed = removed = 0
    for i, (nsuid, info) in enumerate(suspects.items()):
        print(f'\r  {i+1}/{len(suspects)} 修正:{fixed} 移除:{removed}', end='', flush=True)
        body, status, _ = fetch(
            f'https://ec.nintendo.com/HK/zh/titles/{nsuid}', retries=1, timeout=15)
        if status == 404:
            matched.pop(nsuid, None)
            unmatched.append({'nsuid': nsuid, 'title': info['name'],
                              'reason': 'not_found',
                              'last_checked': time.strftime('%Y-%m-%dT%H:%M:%SZ')})
            removed += 1
            continue
        if not body or status != 200:
            continue
        page = extract_base_page(body.decode('utf-8', errors='replace'))
        if not page or not page.get('tid'):
            continue
        page_tid = page['tid']
        if page_tid != info['tid']:
            entry = matched.get(nsuid, [info['tid'], info['name'], '', ''])
            entry[0] = page_tid
            if page.get('name'):
                entry[1] = page['name']
            if page.get('date'):
                entry[2] = page['date']
            if page.get('languages'):
                entry[3] = ', '.join(page['languages'])
            matched[nsuid] = entry
            fixed += 1
    print(f'\r  可疑条目 {len(suspects)}/{len(suspects)}  修正:{fixed}  移除:{removed}')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description='HK NSUID → TID 映射管道')
    p.add_argument('--skip-price', action='store_true', help='跳过 Price API 扫描')
    args = p.parse_args()
    main(args.skip_price)
