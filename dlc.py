import json
import re
import time
from pathlib import Path
from shared.http import fetch

JP_API = 'https://store-jp.nintendo.com/s/MNS/dw/shop/v23_2/product_search'
JP_CID = '965f0a0e-7133-43b7-863c-413e4f6977d7'
JP_ROWS = 200

EU_SOLR = 'https://searching.nintendo-europe.com/en/select'
EU_ROWS = 1000

PRICE_API = 'https://api.ec.nintendo.com/v1/price'
EC_AOCS = 'https://ec.nintendo.com/HK/zh/aocs/{}'


def run(
    pool_path: Path,
    dlc_nsuids: set[str],
    cache_dir: Path,
    us_raw_path: Path | None = None,
    skip_price: bool = False,
) -> tuple[set[int], dict[str, dict]]:
    pool = _load_pool(pool_path)

    # ① HK titledb DLC
    pool -= {int(n) for n in dlc_nsuids}
    print(f'  HK titledb: 池 {len(pool)}')

    # ②③④ JP/EU/US
    jp = _jp_dlc()
    pool -= jp
    print(f'  JP: 池 {len(pool)}')
    eu = _eu_dlc()
    pool -= eu
    print(f'  EU: 池 {len(pool)}')
    us = _us_dlc(us_raw_path)
    if us:
        pool -= us
        print(f'  US: 池 {len(pool)}')

    # 检查点保存（Price API 前）
    _save_pool(pool_path, pool)

    # ⑤ Price API
    new_dlc: set[int] = set()
    if not skip_price:
        new_dlc, pool = _price_scan(pool)

    # ⑥ /aocs/ 爬取 — 失败的重新入池下次重试
    meta: dict[str, dict] = {}
    if new_dlc:
        meta = _crawl_meta(new_dlc)
        (cache_dir / 'hk_dlc_meta.json').write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), 'utf-8')
        failed = new_dlc - {int(n) for n in meta}
        if failed:
            pool.update(failed)
            print(f'  /aocs/ 失败 {len(failed)} 条已重新入池')

    _save_pool(pool_path, pool)
    (cache_dir / 'hk_dlc_new.json').write_text(
        json.dumps(sorted(new_dlc), ensure_ascii=False), 'utf-8')

    return new_dlc, meta


# ---- 池 I/O ----

def _load_pool(path: Path) -> set[int]:
    if not path.exists():
        return set()
    return {int(line.strip()) for line in path.read_text().splitlines() if line.strip().isdigit()}


def _save_pool(path: Path, pool: set[int]):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f'{path.name}.tmp'
    tmp.write_text('\n'.join(str(n) for n in sorted(pool)) + '\n', 'utf-8')
    tmp.replace(path)


# ---- JP OCAPI ----

def _jp_dlc() -> set[int]:
    base = (f'{JP_API}?client_id={JP_CID}&siteId=MNS&locale=ja-JP'
            f'&currency=JPY&refine_1=cgid=software&refine_2=c_softType=AOC'
            f'&sort=new-arrival&count={JP_ROWS}')
    first = _fetch_json(f'{base}&start=0')
    if not first:
        return set()
    total = first.get('total', 0)
    result: set[int] = set()
    for h in first.get('hits', []):
        pid = h.get('product_id', '').lstrip('D')
        if pid.startswith('7005'):
            result.add(int(pid))
    pages = (total + JP_ROWS - 1) // JP_ROWS
    print(f'\r  JP 1/{pages}', end='', flush=True)
    for pg, offset in enumerate(range(JP_ROWS, total, JP_ROWS), 2):
        print(f'\r  JP {pg}/{pages}', end='', flush=True)
        data = _fetch_json(f'{base}&start={offset}')
        if data:
            for h in data.get('hits', []):
                pid = h.get('product_id', '').lstrip('D')
                if pid.startswith('7005'):
                    result.add(int(pid))
    print(f'\r  JP: {len(result)} DLC')
    return result


# ---- EU Solr ----

def _eu_dlc() -> set[int]:
    fq = 'type:DLC'
    url = (f'{EU_SOLR}?q=*:*&fq={fq}&rows={EU_ROWS}'
           f'&start=0&wt=json&fl=nsuid_txt')
    first = _fetch_json(url)
    if not first:
        return set()
    total = first.get('response', {}).get('numFound', 0)
    result: set[int] = set()
    for doc in first['response']['docs']:
        for n in doc.get('nsuid_txt', []):
            if n.startswith('7005') and n.isdigit():
                result.add(int(n))
    pages = (total + EU_ROWS - 1) // EU_ROWS
    print(f'\r  EU 1/{pages}', end='', flush=True)
    for pg, start in enumerate(range(EU_ROWS, total, EU_ROWS), 2):
        print(f'\r  EU {pg}/{pages}', end='', flush=True)
        data = _fetch_json(
            f'{EU_SOLR}?q=*:*&fq={fq}&rows={EU_ROWS}&start={start}&wt=json&fl=nsuid_txt')
        if data:
            for doc in data['response']['docs']:
                for n in doc.get('nsuid_txt', []):
                    if n.startswith('7005') and n.isdigit():
                        result.add(int(n))
    print(f'\r  EU: {len(result)} DLC')
    return result


# ---- US ----

def _us_dlc(raw_path: Path | None) -> set[int]:
    if not raw_path or not raw_path.exists():
        return set()
    with open(raw_path, encoding='utf-8') as f:
        data = json.load(f)
    return {int(h['nsuid']) for h in data
            if h.get('nsuid') and str(h['nsuid']).startswith('7005')}


# ---- Price API ----

def _price_scan(pool: set[int]) -> tuple[set[int], set[int]]:
    hits: set[int] = set()
    ids_list = sorted(pool)
    batches = (len(ids_list) + 49) // 50
    for batch_n, i in enumerate(range(0, len(ids_list), 50), 1):
        batch = ids_list[i:i + 50]
        ids_str = ','.join(str(n) for n in batch)
        data = _fetch_json(f'{PRICE_API}?country=HK&ids={ids_str}&lang=jp')
        if data:
            for p in data.get('prices', []):
                status = p.get('sales_status', '')
                nsuid = p.get('title_id')
                if isinstance(nsuid, int) and status not in ('not_found', 'sales_termination'):
                    hits.add(nsuid)
        print(f'\r  Price API {batch_n}/{batches}  命中:{len(hits)}', end='', flush=True)
        time.sleep(0.06)
    if batches:
        print()
    pool.difference_update(hits)
    return hits, pool


# ---- /aocs/ 爬取 ----

def _crawl_meta(nsuids: set[int]) -> dict[str, dict]:
    result: dict[str, dict] = {}
    ids = sorted(nsuids)
    for i, nsuid in enumerate(ids):
        meta = _extract_dlc_meta(EC_AOCS.format(nsuid))
        if meta:
            result[str(nsuid)] = meta
        if (i + 1) % 10 == 0:
            print(f'\r  /aocs/ {i + 1}/{len(ids)}  成功:{len(result)}', end='', flush=True)
        time.sleep(0.15)
    if ids:
        print(f'\r  /aocs/ {len(ids)}/{len(ids)}  成功:{len(result)}')
    return result


def _extract_dlc_meta(url: str) -> dict | None:
    """从 DLC 页面提取 DlcItem 元数据。纯字符串查找, 不依赖 xmlre。"""
    body, status, _ = fetch(url, retries=3, timeout=15)
    if body is None or status >= 400:
        return None
    html = body.decode("utf-8", errors="replace")

    target = url.rsplit("/", 1)[-1]
    # 定位: \"nsUid\":TARGET
    idx = html.find(r'\"nsUid\":' + target)
    if idx < 0:
        return None

    # DLC 名称: 往前找最近的 \"formalName\":\"
    fm = r'\"formalName\":\"'
    last_fm = html[:idx].rfind(fm)
    if last_fm < 0:
        return None
    name_start = last_fm + len(fm)
    name_end = html.find(r'\"', name_start)
    name = html[name_start:name_end] if name_end > 0 else ""
    name = re.sub(r'\\u([0-9a-fA-F]{4})', lambda m: chr(int(m.group(1), 16)), name)

    # 父 BASE: baseApplicationItemNsUid(升级包) 或 targetApplicationItems[0].nsUid(普通DLC)
    parent = ""
    m = re.search(r'baseApplicationItemNsUid\\\":(\d+)', html)
    if m and m.group(1).startswith('7001'):
        parent = m.group(1)
    if not parent:
        tai = html.find(r'targetApplicationItems')
        if tai >= 0:
            m = re.search(r'\\"nsUid\\":(7001\d+)', html[tai:tai+500])
            if m:
                parent = m.group(1)

    # 发售日: \"releaseDateOnEshop\":\"YYYY-MM-DD\"
    date = ""
    d_start = html.find(r'\"releaseDateOnEshop\":\"')
    if d_start >= 0:
        d_val_start = d_start + len(r'\"releaseDateOnEshop\":\"')
        d_val_end = html.find(r'\"', d_val_start)
        if d_val_end > 0 and (d_val_end - d_val_start) < 20:
            date = html[d_val_start:d_val_end]

    # 发行商: \"publisher\":{\"name\":\"PUB\"
    publisher = ""
    pub_start = html.find(r'\"publisher\":{\"name\":\"')
    if pub_start >= 0:
        pub_val_start = pub_start + len(r'\"publisher\":{\"name\":\"')
        pub_val_end = html.find(r'\"', pub_val_start)
        if pub_val_end > 0:
            publisher = html[pub_val_start:pub_val_end]

    return {"name": name, "date": date, "publisher": publisher,
            "parent_nsuid": parent}



def _fetch_json(url: str) -> dict | None:
    body, status, _ = fetch(url, retries=3, timeout=15)
    if body is None or status >= 400:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None
