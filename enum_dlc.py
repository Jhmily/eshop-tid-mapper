#!/usr/bin/env python3
"""HK DLC 枚举 — 每日清洗枚举池, 输出新上架 DLC NSUID 列表."""

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
DIR = Path(__file__).parent
CACHE = DIR / ".cache"
OUT = DIR / "output"
POOL_FILE = CACHE / "enum_dlc_pool.txt"
TITLEDB = CACHE / "HK.zh.json"
US_RAW = CACHE / "raw_full.json"

JP_API = "https://store-jp.nintendo.com/s/MNS/dw/shop/v23_2/product_search"
JP_CID = "965f0a0e-7133-43b7-863c-413e4f6977d7"
EU_SOLR = "https://searching.nintendo-europe.com/en/select"
PRICE_API = "https://api.ec.nintendo.com/v1/price"


def _proxy() -> str | None:
    try:
        import winreg
    except ImportError:
        return None
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
        if not enabled:
            winreg.CloseKey(key)
            return None
        server, _ = winreg.QueryValueEx(key, "ProxyServer")
        winreg.CloseKey(key)
        if not server:
            return None
        server = server.replace(" ", "")
        if "=" in server:
            for part in server.split(";"):
                if "=" in part:
                    _, val = part.split("=", 1)
                    if val:
                        return f"http://{val}"
        return f"http://{server}"
    except Exception:
        return None


def _setup():
    p = _proxy()
    if p:
        handler = urllib.request.ProxyHandler({"http": p, "https": p})
        urllib.request.install_opener(urllib.request.build_opener(handler))
    for d in (CACHE, OUT):
        d.mkdir(parents=True, exist_ok=True)
    return p


def _fetch(url: str, timeout: int = 15, retries: int = 3) -> dict | None:
    """GET JSON, 失败重试, 返回 None 表示不可恢复."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            last_err = e
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            last_err = e
        if attempt < retries:
            time.sleep(attempt * 0.5)
    print(f"  fetch fail: {url[:100]} - {last_err}")
    return None


def _load_pool() -> set[int]:
    if not POOL_FILE.exists():
        return set()
    with open(POOL_FILE) as f:
        return {int(line.strip()) for line in f}


def _save_pool(pool: set[int]):
    with open(POOL_FILE, "w") as f:
        for n in sorted(pool):
            f.write(f"{n}\n")


def _hk_titledb_dlc() -> set[int]:
    """从本地 HK.zh.json 提取 7005 NSUID."""
    if not TITLEDB.exists():
        return set()
    with open(TITLEDB, encoding="utf-8") as f:
        data = json.load(f)
    result: set[int] = set()
    for k, v in data.items():
        if k.startswith("7005") and k[4:].isdigit():
            tid = v.get("id", "")
            if isinstance(tid, str) and len(tid) == 16:
                result.add(int(k))
    return result


def _jp_dlc() -> set[int]:
    """OCAPI product_search 全量翻页, 提取 7005 NSUID."""
    base = (f"{JP_API}?client_id={JP_CID}&siteId=MNS&locale=ja-JP"
            f"&currency=JPY&refine_1=cgid=software&sort=new-arrival&count=200")
    first = _fetch(base + "&start=0")
    if not first:
        return set()
    total = first["total"]
    page_ids: set[str] = set()
    for h in first.get("hits", []):
        nsuid = h["product_id"].lstrip("D")
        if nsuid.startswith("7005"):
            page_ids.add(nsuid)

    def _page(offset: int) -> set[str]:
        data = _fetch(f"{base}&start={offset}")
        if not data:
            return set()
        return {h["product_id"].lstrip("D") for h in data.get("hits", [])
                if h["product_id"].lstrip("D").startswith("7005")}

    offsets = list(range(200, total, 200))
    if offsets:
        with ThreadPoolExecutor(max_workers=6) as ex:
            futures = {ex.submit(_page, o): o for o in offsets}
            for f in as_completed(futures):
                page_ids |= f.result()
    return {int(n) for n in page_ids}


def _eu_dlc() -> set[int]:
    """Solr type:DLC, 提取 7005 NSUID."""
    fq = quote("type:DLC")
    fl = "nsuid_txt"
    url = f"{EU_SOLR}?q=*&fq={fq}&rows=1000&start=0&wt=json&fl={fl}"
    first = _fetch(url)
    if not first:
        return set()
    total = first["response"]["numFound"]
    result: set[int] = set()
    for doc in first["response"]["docs"]:
        for n in doc.get("nsuid_txt", []):
            if n.startswith("7005") and n.isdigit():
                result.add(int(n))

    def _page(start: int) -> set[int]:
        u = f"{EU_SOLR}?q=*&fq={fq}&rows=1000&start={start}&wt=json&fl={fl}"
        data = _fetch(u)
        if not data:
            return set()
        page_set: set[int] = set()
        for doc in data["response"]["docs"]:
            for n in doc.get("nsuid_txt", []):
                if n.startswith("7005") and n.isdigit():
                    page_set.add(int(n))
        return page_set

    offsets = list(range(1000, total, 1000))
    if offsets:
        with ThreadPoolExecutor(max_workers=6) as ex:
            futures = {ex.submit(_page, o): o for o in offsets}
            for f in as_completed(futures):
                result |= f.result()
    return result


def _us_dlc() -> set[int]:
    """读 CI 同次运行产出的 raw_full.json, 提取 7005 NSUID."""
    if not US_RAW.exists():
        return set()
    with open(US_RAW, encoding="utf-8") as f:
        data = json.load(f)
    return {int(h["nsuid"]) for h in data
            if h.get("nsuid") and str(h["nsuid"]).startswith("7005")}


def _price_scan(pool: set[int]) -> set[int]:
    """单线程顺序扫 Price API, 返回命中的 NSUID."""
    hits: set[int] = set()
    ids_list = sorted(pool)
    total_batches = (len(ids_list) + 49) // 50
    for batch_n, i in enumerate(range(0, len(ids_list), 50), 1):
        batch = ids_list[i:i + 50]
        ids_str = ",".join(str(n) for n in batch)
        data = _fetch(f"{PRICE_API}?country=HK&ids={ids_str}&lang=jp")
        if data:
            for p in data.get("prices", []):
                if p.get("sales_status") not in ("not_found", "sales_termination"):
                    hits.add(p["title_id"])
        print(f"\r  Price API {batch_n}/{total_batches}  命中: {len(hits)}",
              end="", flush=True)
        time.sleep(0.06)
    print()
    return hits


def main():
    parser = argparse.ArgumentParser(description="HK DLC 枚举")
    parser.add_argument("--skip-price", action="store_true", help="跳过 Price API")
    args = parser.parse_args()

    t0 = time.time()
    proxy = _setup()
    print(f"=== HK DLC 枚举 ===")

    pool = _load_pool()
    print(f"  枚举池: {len(pool)}")

    hk = _hk_titledb_dlc()
    rm = len(pool & hk)
    pool -= hk
    print(f"  HK titledb: {len(hk)} DLC, 池移除 {rm}, 剩余 {len(pool)}")

    print("  JP/EU/US 并行拉取 ...")
    with ThreadPoolExecutor(max_workers=3) as ex:
        f_jp = ex.submit(_jp_dlc)
        f_eu = ex.submit(_eu_dlc)
        f_us = ex.submit(_us_dlc)
        jp, eu, us = f_jp.result(), f_eu.result(), f_us.result()

    for label, dlc_set in [("JP", jp), ("EU", eu), ("US", us)]:
        rm = len(pool & dlc_set)
        pool -= dlc_set
        print(f"  {label}: {len(dlc_set)} DLC, 池移除 {rm}, 剩余 {len(pool)}")

    new_dlcs: set[int] = set()
    if not args.skip_price:
        print("  Price API 扫描 ...")
        hits = _price_scan(pool)
        pool -= hits
        new_dlcs = hits

    _save_pool(pool)
    if new_dlcs:
        new_file = CACHE / "hk_dlc_new.json"
        new_file.write_text(json.dumps(sorted(new_dlcs), ensure_ascii=False), "utf-8")
        print(f"  新 DLC: {len(new_dlcs)} -> {new_file}")

    print(f"  完成 ({time.time() - t0:.0f}s)")


if __name__ == "__main__":
    main()
