import json
import re
from pathlib import Path
from shared.http import download_with_etag

TITLEDB_URL = 'https://raw.githubusercontent.com/blawar/titledb/refs/heads/master/HK.zh.json'
NS2E_RE = re.compile(r'Nintendo\s*Switch\s*2', re.IGNORECASE)


def download(cache_path: Path) -> bool:
    return download_with_etag(TITLEDB_URL, cache_path)


def process(cache_path: Path) -> tuple[dict, dict, set[str]]:
    with open(cache_path, encoding='utf-8') as f:
        raw = json.load(f)
    base = _extract_and_clean_base(raw)
    dlc = _extract_and_clean_dlc(raw)
    return base, dlc, set(dlc.keys())


def _extract_and_clean_base(raw: dict) -> dict[str, dict]:
    entries: dict[str, dict] = {}
    for nsuid, v in raw.items():
        if not nsuid.startswith('7001'):
            continue
        tid = v.get('id', '')
        if not isinstance(tid, str) or len(tid) != 16:
            continue
        if tid[-3:] != '000':
            continue
        name = v.get('name', '') or ''
        if v.get('isDemo'):
            continue
        if NS2E_RE.search(name):
            continue
        entries[nsuid] = {
            'tid': tid.upper(),
            'name': name,
            'release_date': str(v.get('releaseDate', '') or ''),
            'languages': v.get('languages') if isinstance(v.get('languages'), list) else [],
            'publisher': v.get('publisher') or '',
            'size': v.get('size') or 0,
            'number_of_players': v.get('numberOfPlayers') or 0,
            'rights_id': v.get('rightsId') or '',
        }
    return _resolve_conflicts(entries, raw, is_base=True)


def _extract_and_clean_dlc(raw: dict) -> dict[str, dict]:
    entries: dict[str, dict] = {}
    for nsuid, v in raw.items():
        if not nsuid.startswith('7005'):
            continue
        tid = v.get('id', '')
        if not isinstance(tid, str) or len(tid) != 16:
            continue
        if tid[-3:] == '000':
            continue
        entries[nsuid] = {
            'tid': tid.upper(),
            'name': v.get('name', '') or '',
            'release_date': str(v.get('releaseDate', '') or ''),
            'publisher': v.get('publisher') or '',
            'size': v.get('size') or 0,
            'rights_id': v.get('rightsId') or '',
        }
    return _resolve_conflicts(entries, raw, is_base=False)


def _resolve_conflicts(entries: dict, raw: dict, is_base: bool) -> dict[str, dict]:
    groups: dict[str, list[str]] = {}
    for nsuid, e in entries.items():
        groups.setdefault(e['tid'], []).append(nsuid)

    clean: dict[str, dict] = {}
    for tid, nsuids in groups.items():
        if len(nsuids) == 1:
            clean[nsuids[0]] = entries[nsuids[0]]
            continue

        if is_base:
            rules = [_rule_ns2e, _rule_shorter_name, _rule_null_stub]
        else:
            rules = [_rule_null_stub]

        remaining = nsuids[:]
        changed = True
        while len(remaining) > 1 and changed:
            changed = False
            for rule in rules:
                prev = len(remaining)
                remaining = rule(remaining, entries, raw)
                if len(remaining) < prev:
                    changed = True
                if len(remaining) == 1:
                    break

        if len(remaining) == 1:
            clean[remaining[0]] = entries[remaining[0]]
            continue

        for nsuid in remaining:
            entry = dict(entries[nsuid])
            entry['_conflict'] = True
            clean[nsuid] = entry

    return clean


def _rule_ns2e(nsuids: list[str], entries: dict, raw: dict) -> list[str]:
    keep, toss = [], []
    for n in nsuids:
        (toss if NS2E_RE.search(entries[n]['name']) else keep).append(n)
    return keep if keep else nsuids


def _rule_shorter_name(nsuids: list[str], entries: dict, _raw: dict = None) -> list[str]:
    if len(nsuids) != 2:
        return nsuids
    n1, n2 = entries[nsuids[0]]['name'], entries[nsuids[1]]['name']
    if n1 in n2:
        return [nsuids[0]]
    if n2 in n1:
        return [nsuids[1]]
    n1s, n2s = n1.replace(' ', ''), n2.replace(' ', '')
    if n1s in n2s:
        return [nsuids[0]]
    if n2s in n1s:
        return [nsuids[1]]
    return nsuids


def _rule_null_stub(nsuids: list[str], entries: dict, raw: dict) -> list[str]:
    if len(nsuids) < 2:
        return nsuids
    by_name: dict[str, list[str]] = {}
    for n in nsuids:
        by_name.setdefault(entries[n]['name'], []).append(n)
    result: list[str] = []
    for name, group in by_name.items():
        if len(group) == 1:
            result.extend(group)
            continue
        def _null_score(n):
            r = raw.get(n, {})
            return sum(1 for k in ('publisher', 'releaseDate', 'screenshots', 'rightsId')
                       if not r.get(k))
        group.sort(key=_null_score)
        result.append(group[0])
    return result


