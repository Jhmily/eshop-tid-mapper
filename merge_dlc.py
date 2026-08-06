#!/usr/bin/env python3
"""合并 BASE + DLC -> hk_full.json"""

import json
from pathlib import Path

DIR = Path(__file__).parent
CACHE = DIR / ".cache"
OUT = DIR / "output"
BASE_MAP = OUT / "hk_tid_map.json"
DLC_META = CACHE / "hk_dlc_meta.json"
TITLEDB = CACHE / "HK.zh.json"
FULL_OUT = OUT / "hk_full.json"


def _derive_parent(tid: str) -> str:
    """DLC TID -> 父 BASE TID, 如 0100XXXF001 -> 0100XXXE000"""
    if len(tid) != 16 or tid[-3:] == "000":
        return ""
    try:
        b = int(tid[-4], 16)
        return tid[:-4] + hex(b - 1)[2:].upper() + "000" if b > 0 else ""
    except Exception:
        return ""


def main():
    if not BASE_MAP.exists():
        print(f"  {BASE_MAP} 不存在, 先运行 hk_pipeline.py")
        return
    if not TITLEDB.exists():
        print(f"  {TITLEDB} 不存在, 先下载 titledb")
        return
    with open(BASE_MAP, encoding="utf-8") as f:
        base_map: dict = json.load(f)

    with open(TITLEDB, encoding="utf-8") as f:
        tdb = json.load(f)

    # 构建 BASE TID -> [NSUIDs] (titledb + base_map双源, NS1和NS2E都挂DLC)
    tid_to_nsuids: dict[str, list[str]] = {}
    for nsuid, entry in base_map.items():
        tid = entry[0] if entry else ""
        if len(tid) == 16:
            tid_to_nsuids.setdefault(tid, []).append(nsuid)
    for nsuid, v in tdb.items():
        tid = v.get("id", "")
        if not isinstance(tid, str) or len(tid) != 16:
            continue
        tid = tid.upper()
        if nsuid not in tid_to_nsuids.get(tid, []):
            tid_to_nsuids.setdefault(tid, []).append(nsuid)

    # titledb DLC: TID 推导父, 挂到所有同名TID的BASE
    tdb_dlcs: dict[str, list] = {}
    for nsuid, v in tdb.items():
        if not nsuid.startswith("7005"):
            continue
        tid = v.get("id", "")
        if not isinstance(tid, str) or len(tid) != 16:
            continue
        tid = tid.upper()
        parent_tid = _derive_parent(tid)
        name = v.get("name", "")
        date = str(v.get("releaseDate", ""))
        for parent_nsuid in tid_to_nsuids.get(parent_tid, []):
            tdb_dlcs.setdefault(parent_nsuid, []).append(
                [nsuid, tid, name, date]
            )

    # 爬虫 DLC: 已带 parent_nsuid
    crawled: dict[str, list] = {}
    if DLC_META.exists():
        with open(DLC_META, encoding="utf-8") as f:
            meta = json.load(f)
        for nsuid, info in meta.items():
            parent = info.get("parent_nsuid", "")
            if not parent:
                continue
            crawled.setdefault(parent, []).append(
                [nsuid, "暂无", info.get("name", ""), info.get("date", "")]
            )

    # 合并: BASE map 里添加 dlcs 字段
    result: dict = {}
    for nsuid, entry in base_map.items():
        new_entry = list(entry)
        dlcs = []
        if nsuid in tdb_dlcs:
            dlcs.extend(tdb_dlcs[nsuid])
        if nsuid in crawled:
            for d in crawled[nsuid]:
                if d not in dlcs:
                    dlcs.append(d)
        dlcs.sort(key=lambda x: (x[3] or "9999-99-99", int(x[0])))
        langs = entry[3] if len(entry) > 3 else ""
        new_entry = [entry[0], entry[1], entry[2], langs]
        if dlcs:
            new_entry.append({"dlcs": dlcs})
        result[nsuid] = new_entry

    with open(FULL_OUT, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"  hk_full.json: {len(result)} BASE + DLC")


if __name__ == "__main__":
    main()
