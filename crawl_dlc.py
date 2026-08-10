#!/usr/bin/env python3
"""HK DLC 元数据爬取 — 从 ec.nintendo.com 页面提取名称/日期/父游戏"""

import argparse
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
DIR = Path(__file__).parent
CACHE = DIR / ".cache"
OUT = DIR / "output"
NEW_DLC = CACHE / "hk_dlc_new.json"
META_OUT = CACHE / "hk_dlc_meta.json"
EC = "https://ec.nintendo.com/HK/zh/aocs/{}"



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


def _fetch_html(url: str, retries: int = 3) -> str | None:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except (urllib.error.HTTPError, urllib.error.URLError,
                OSError, TimeoutError):
            if attempt < retries:
                time.sleep(attempt * 0.5)
    return None


def _extract(html: str, target_nsuid: str) -> dict | None:
    """从 DLC 页面提取 DlcItem 元数据。纯字符串查找, 不依赖 xmlre。"""
    # 定位: \"nsUid\":TARGET
    idx = html.find(r'\"nsUid\":' + target_nsuid)
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


def main():
    parser = argparse.ArgumentParser(description="HK DLC 元数据爬取")
    parser.add_argument("--file", help="指定 DLC 列表文件 (默认 hk_dlc_new.json)")
    args = parser.parse_args()

    t0 = time.time()
    proxy = _setup()
    print(f"=== HK DLC 爬虫 ===")

    src = Path(args.file) if args.file else NEW_DLC
    if not src.exists():
        print(f"  文件不存在: {src}")
        return

    with open(src, encoding="utf-8") as f:
        nsuids = json.load(f)
    print(f"  待爬: {len(nsuids)}")

    meta: dict[str, dict] = {}
    failed = 0

    for i, nsuid in enumerate(nsuids):
        nsuid_str = str(nsuid)
        url = EC.format(nsuid_str)
        html = _fetch_html(url)
        if not html:
            failed += 1
            print(f"\r  {i + 1}/{len(nsuids)}  失败: {failed}",
                  end="", flush=True)
            time.sleep(0.15)
            continue

        info = _extract(html, nsuid_str)
        if info:
            meta[nsuid_str] = info
        else:
            failed += 1

        print(f"\r  {i + 1}/{len(nsuids)}  成功: {len(meta)}  失败: {failed}",
              end="", flush=True)
        time.sleep(0.15)

    print()
    META_OUT.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                        "utf-8")
    print(f"  成功: {len(meta)}  失败: {failed} -> {META_OUT}")
    print(f"  完成 ({time.time() - t0:.0f}s)")


if __name__ == "__main__":
    main()
