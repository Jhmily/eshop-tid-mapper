#!/usr/bin/env python3
"""US Algolia 全量抓取: 基础桶 + 溢出桶递归拆分, 并发拉取。
产出: us/raw_full.json (原始 hits, 全字段)
用法: python us_fetch.py
"""
import json
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

APP_ID = 'U3B6GR4UA3'
API_KEY = 'a29c6927638bfd8cee23993e51e721c9'
INDEX = 'store_game_en_us'
URL = f'https://{APP_ID}-dsn.algolia.net/1/indexes/{INDEX}/query'
THREADS = 10
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'

DIR = Path(__file__).parent
OUT = DIR / '.cache' / 'raw_full.json'

RATINGS = ['E', 'T', 'E10', 'M', 'RP']
PRICES = ['$0 - $4.99', '$5 - $9.99', '$10 - $19.99',
          '$20 - $39.99', '$40+', 'Free to start']


def _read_windows_proxy() -> str | None:
    """读 Windows 系统代理 (Clash 开启时写注册表)。"""
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


# 代理: 手动(环境变量 NSUID_FETCH_PROXY) > Windows 系统代理 > 直连
import os
PROXY = os.environ.get('NSUID_FETCH_PROXY') or _read_windows_proxy()

# 连接池 (keep-alive)
SESSION = requests.Session()
if PROXY:
    SESSION.proxies = {'http': PROXY, 'https': PROXY}
    SESSION.trust_env = False  # 有明确代理时禁用环境变量代理, 防冲突
SESSION.headers.update({
    'Content-Type': 'application/json',
    'X-Algolia-Application-Id': APP_ID,
    'X-Algolia-API-Key': API_KEY,
    'User-Agent': UA,
})


def _fetch(params: dict) -> dict:
    """POST 查询 -> 响应 dict; 失败重试 3 次。"""
    for attempt in range(3):
        try:
            r = SESSION.post(URL, json=params, timeout=30)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError):
            if attempt < 2:
                time.sleep((attempt + 1) * 0.5)
    raise RuntimeError(f'fetch failed: {params}')


def _base_params(bucket: dict) -> dict:
    params = {'query': ''}
    if bucket['facets']:
        params['facetFilters'] = [[v] for v in bucket['facets']]
    if bucket.get('filters'):
        params['filters'] = bucket['filters']
    return params


def probe(bucket: dict) -> int:
    """查询桶的 nbHits。"""
    p = _base_params(bucket)
    p.update({'hitsPerPage': 0, 'page': 0})
    return _fetch(p).get('nbHits', 0)


def _has(facets: list[str], prefix: str) -> bool:
    return any(f.startswith(prefix) for f in facets)


def _used(bucket: dict, prefix: str) -> bool:
    """维度是否已被 facets 或 filters 使用 (防重复拆分死循环)。"""
    return (_has(bucket['facets'], prefix)
            or (bucket.get('filters') or '').find(f'{prefix}') >= 0)


def subdivide(bucket: dict) -> list[dict]:
    """溢出桶细分 (固定维度顺序, 全部互斥且覆盖全集):
    regPrice段 -> hasDlc二分 -> playerCount四值+NOT -> nsoFeatures二分 -> publisher前100+NOT"""
    facets, filters = bucket['facets'], bucket.get('filters')
    out = []

    # 1. regPrice 段: 宽度减半递归, 0.01 精度到顶后走其他维度
    rg = bucket.get('rg')
    if rg:
        lo, hi = rg
        if hi - lo > 0.01:
            mid = (lo + hi) / 2
            return [{'facets': facets, 'reg': True, 'rg': (lo, mid),
                     'filters': _and(filters,
                                     f'price.regPrice>={lo} AND price.regPrice<{mid}')},
                    {'facets': facets, 'reg': True, 'rg': (mid, hi),
                     'filters': _and(filters,
                                     f'price.regPrice>={mid} AND price.regPrice<{hi}')}]
        # 0.01 精度到顶 (定价尖峰等值段), 走其他维度
    if not rg and 'price.regPrice' not in (filters or ''):
        # 首次进入价格桶: 生成 regPrice 段 + 值域外兜底
        # (priceRange 按现价分类, regPrice 是原价: 促销游戏原价在段外)
        pr = next((f[len('priceRange:'):] for f in facets
                   if f.startswith('priceRange:')), None)
        lo, hi = _price_span(pr) if pr else (None, None)
        if lo is not None:
            out = [{'facets': facets, 'reg': True, 'rg': (lo, hi),
                    'filters': _and(filters,
                                    f'price.regPrice>={lo} AND price.regPrice<{hi}')}]
            out.append({'facets': facets,
                        'filters': _and(filters,
                                        f'NOT price.regPrice>={lo} OR NOT price.regPrice<{hi}')})
            return out

    # 2. hasDlc 二分
    if not _used(bucket, 'hasDlc:'):
        return [{'facets': facets + ['hasDlc:true'], 'filters': filters, **({'rg': rg} if rg else {})},
                {'facets': facets + ['hasDlc:false'], 'filters': filters, **({'rg': rg} if rg else {})}]

    # 3. playerCount 四值 + NOT 兜底
    if not _used(bucket, 'playerCount:'):
        subs = [{'facets': facets + [f'playerCount:{p}'], 'filters': filters,
                 **({'rg': rg} if rg else {})}
                for p in ['Single player', '2+', '4+', '3+']]
        nots = ' AND '.join(f'NOT playerCount:"{p}"' for p in
                            ['Single player', '2+', '4+', '3+'])
        subs.append({'facets': facets, 'filters': _and(filters, nots),
                     **({'rg': rg} if rg else {})})
        return subs

    # 4. nsoFeatures 二分
    if not _used(bucket, 'nsoFeatures:'):
        return [{'facets': facets, 'filters': _and(filters, 'nsoFeatures:"Save Data Cloud"'),
                 **({'rg': rg} if rg else {})},
                {'facets': facets, 'filters': _and(filters, 'NOT nsoFeatures:"Save Data Cloud"'),
                 **({'rg': rg} if rg else {})}]

    # 5. gameGenres: 各值桶 + NOT 兜底 (多值属性, 重复拉取本地去重)
    if not _used(bucket, 'gameGenres.combined:'):
        p = _base_params(bucket)
        p.update({'hitsPerPage': 0, 'page': 0,
                  'facets': ['gameGenres.combined'], 'maxValuesPerFacet': 100})
        dist = _fetch(p).get('facets', {}).get('gameGenres.combined', {})
        out = [{'facets': facets + [f'gameGenres.combined:{v}'], 'filters': filters,
                **({'rg': rg} if rg else {})}
               for v in dist]
        nots = ' AND '.join(f'NOT gameGenres.combined:"{v}"' for v in dist)
        out.append({'facets': facets, 'filters': _and(filters, nots),
                    **({'rg': rg} if rg else {})})
        return out
    # 维度链穷尽: 告警不静默
    print(f'  [WARN] 桶无法继续拆分: facets={facets[-2:]} filters={str(filters)[:60]}')
    return []


def _price_span(pr: str) -> tuple[int | None, int | None]:
    """'$0 - $4.99' -> (0,5); '$40+' -> (None,None) 不切 regPrice
    (70 上限会漏 79.99 的 NS2 大作, 直接走 hasDlc 维度链更安全)"""
    if pr.endswith('+'):
        return None, None
    m = pr.replace('$', '').split(' - ')
    if len(m) == 2 and m[0].replace('.', '').isdigit() and m[1].replace('.', '').isdigit():
        return int(float(m[0])), int(float(m[1])) + 1
    return None, None


def expand_all(start: list[dict]) -> list[dict]:
    """BFS 分层: 同层并发探测 nbHits, >1000 拆, 直到全部叶子。"""
    leaves: list[dict] = []
    level = start
    depth = 0
    while level:
        depth += 1
        next_level: list[dict] = []
        with ThreadPoolExecutor(max_workers=THREADS) as ex:
            futures = {ex.submit(probe, b): b for b in level}
            for f in as_completed(futures):
                b = futures[f]
                if f.result() > 1000:
                    next_level.extend(subdivide(b))
                else:
                    leaves.append(b)
        print(f'  展开层 {depth}: {len(level)} 桶 -> {len(next_level)} 子桶, '
              f'叶子累计 {len(leaves)}', flush=True)
        level = next_level
        if depth >= 30:
            print('  [WARN] 展开超过 30 层, 强制截断')
            for b in level[:5]:
                print(f'    无法拆分的桶: facets={b["facets"][-3:]} '
                      f'rg={b.get("rg")} filters={str(b.get("filters"))[:120]}')
            break
    return leaves


def fetch_bucket(bucket: dict) -> tuple[list, int]:
    """拉取一个叶子桶 -> (hits, nbHits)。"""
    p = _base_params(bucket)
    p.update({'hitsPerPage': 1000, 'page': 0})
    r = _fetch(p)
    return r.get('hits', []), r.get('nbHits', 0)


def _and(a: str | None, b: str) -> str:
    return f'{a} AND {b}' if a else b


def _not_chain(values: list[str], attr: str) -> str:
    return ' AND '.join(f'NOT {attr}:"{v}"' for v in values)


def build_buckets() -> list[dict]:
    """基础桶 (维度组合) + 新值 NOT 兜底桶 (当前为空, 新评级/平台出现时自动覆盖)"""
    buckets = []
    for r in RATINGS:
        buckets.append({'facets': ['platform:Nintendo Switch 2', f'contentRatingCode:{r}']})
    buckets.append({'facets': ['platform:Nintendo Switch 2'],
                    'filters': _not_chain(RATINGS, 'contentRatingCode')})
    buckets.append({'facets': ['platform:iOS/Android']})
    buckets.append({'facets': [], 'filters': _not_chain(PRICES, 'priceRange')})
    buckets.append({'facets': [],
                    'filters': _not_chain(['Nintendo Switch', 'Nintendo Switch 2',
                                           'iOS/Android'], 'platform')})
    for r in RATINGS:
        for p in PRICES:
            buckets.append({'facets': ['platform:Nintendo Switch',
                                       f'contentRatingCode:{r}',
                                       f'priceRange:{p}']})
    buckets.append({'facets': ['platform:Nintendo Switch'],
                    'filters': _not_chain(RATINGS, 'contentRatingCode')})
    return buckets


def main():
    t0 = time.time()
    buckets = build_buckets()
    print(f'并发: {THREADS}, 基础桶: {len(buckets)} 个')

    print('[2] 展开桶树 ...')
    leaves = expand_all(buckets)
    print(f'  叶子桶: {len(leaves)} 个, 耗时 {time.time()-t0:.0f}s')

    print('[3] 并发拉取 (自动重拆, 最多 3 轮) ...')
    all_hits: dict[str, dict] = {}
    pending = leaves
    for rnd in range(1, 4):
        if not pending:
            break
        redo: list[dict] = []
        done = 0
        with ThreadPoolExecutor(max_workers=THREADS) as ex:
            futures = {ex.submit(fetch_bucket, b): b for b in pending}
            for f in as_completed(futures):
                b = futures[f]
                hits, nb = f.result()
                for h in hits:
                    all_hits.setdefault(h.get('nsuid'), h)
                if len(hits) < nb:
                    redo.append(b)
                done += 1
                print(f'\r  轮{rnd} {done}/{len(pending)} 桶, 唯一 nsuid: {len(all_hits)}',
                      end='', flush=True)
        print()
        if redo:
            pending = [s for b in redo for s in expand_all([b])]
            print(f'  截断 {len(redo)} 桶 -> 重拆 {len(pending)} 桶 (轮 {rnd+1})')
        else:
            pending = []
    if pending:
        print(f'  [WARN] 仍有 {len(pending)} 桶未拉全 (数据增长过快)')

    # 完整性对照
    baseline = _fetch({'query': '', 'hitsPerPage': 0, 'page': 0}).get('nbHits', 0)
    print(f'基线 nbHits: {baseline}, 抓取: {len(all_hits)}, 缺口: {baseline - len(all_hits)}')

    OUT.write_text(json.dumps(list(all_hits.values()), ensure_ascii=False), 'utf-8')
    size_mb = OUT.stat().st_size / 1024 / 1024
    print(f'完成: {len(all_hits)} 款, {size_mb:.0f}MB, {time.time()-t0:.0f}s -> {OUT}')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f'\n出错: {e}')
    import os
    if not os.environ.get('CI') and not os.environ.get('GITHUB_ACTIONS'):
        input('\n按回车退出...')
