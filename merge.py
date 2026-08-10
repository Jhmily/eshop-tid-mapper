import json
import time
from pathlib import Path
from shared.http import fetch
from shared.tid import extract_tid, iso_to_english

EC_URL = 'https://ec.nintendo.com/HK/zh/titles/{}'
EC_TIMEOUT = 15
EC_RETRIES = 3


def run(
    titledb_base: dict[str, dict],
    api_base: dict[str, dict],
    prev_map_path: Path | None = None,
    state_path: Path | None = None,
) -> tuple[dict[str, list], list[dict]]:
    prev_map = _load_json(prev_map_path) if prev_map_path else {}
    state_404 = _load_state(state_path)

    # 3A: titledb 给 TID，API 给元数据。API 没有则回退 titledb
    matched: dict[str, list] = {}
    for nsuid, e in titledb_base.items():
        api_info = api_base.get(nsuid, {})
        matched[nsuid] = [
            e['tid'],
            api_info.get('title', '') or e['name'],
            api_info.get('release_date', '') or _date_fmt(e.get('release_date', '')),
            api_info.get('languages', '') or iso_to_english(e.get('languages', [])),
        ]

    # 3B: 历史输出保底
    for nsuid, entry in prev_map.items():
        if nsuid not in matched and len(entry) >= 4:
            matched[nsuid] = entry

    # 3C: API 独有 → to_scrape
    to_scrape = [n for n in api_base if n not in matched]

    # 4A: ec.nintendo 补漏
    unmatched: list[dict] = []
    total = len(to_scrape)
    ok = nf = nt = 0
    for i, nsuid in enumerate(to_scrape):
        if nsuid in state_404:
            nf += 1; unmatched.append(_mk_unmatched(nsuid, api_base, 'not_found'))
            continue
        tid = _scrape_one(nsuid)
        if tid:
            ok += 1; info = api_base[nsuid]
            matched[nsuid] = [tid, info['title'], info['release_date'], info['languages']]
        elif tid is False:
            nf += 1; state_404.add(nsuid)
            unmatched.append(_mk_unmatched(nsuid, api_base, 'not_found'))
        else:
            nt += 1; unmatched.append(_mk_unmatched(nsuid, api_base, 'no_tid_yet'))
        if (i + 1) % 10 == 0 or i + 1 == total:
            print(f'\r  ec.nintendo {i+1}/{total}  TID:{ok}  404:{nf}  未发售:{nt}', end='', flush=True)
    if total:
        print()

    if state_path:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({
            n: {'404_since': time.strftime('%Y-%m-%d')} for n in sorted(state_404)
        }, ensure_ascii=False, indent=2), 'utf-8')

    return matched, unmatched


def _scrape_one(nsuid: str) -> str | None | bool:
    # -> tid, None (无TID), False (404)
    for attempt in range(EC_RETRIES):
        try:
            body, status, _ = fetch(EC_URL.format(nsuid), retries=1, timeout=EC_TIMEOUT)
            if status == 404:
                return False
            if body is None or status >= 400:
                if attempt < EC_RETRIES - 1:
                    time.sleep(attempt * 0.5)
                continue
            tid = extract_tid(body.decode('utf-8', errors='replace'))
            return tid if tid else None
        except Exception:
            if attempt < EC_RETRIES - 1:
                time.sleep(attempt * 0.5)
    return None


def _load_json(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text('utf-8'))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _load_state(path: Path | None) -> set[str]:
    if path and path.exists():
        try:
            return set(json.loads(path.read_text('utf-8')).keys())
        except (json.JSONDecodeError, OSError):
            pass
    return set()


def _mk_unmatched(nsuid: str, api_base: dict[str, dict], reason: str) -> dict:
    info = api_base.get(nsuid, {})
    return {
        'nsuid': nsuid,
        'title': info.get('title', ''),
        'reason': reason,
        'last_checked': time.strftime('%Y-%m-%dT%H:%M:%SZ'),
    }


def _date_fmt(d: str) -> str:
    d = (d or '').strip()
    if len(d) == 8 and d.isdigit():
        return f'{d[:4]}-{d[4:6]}-{d[6:8]}'
    return d[:10] if d else ''


