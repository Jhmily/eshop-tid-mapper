import json
from pathlib import Path
from shared.http import fetch

HK_API = 'https://www.nintendo.com/hk/api/search'
_MAX_SIZE = 10_000  # API 单页上限


def pull(cache_path: Path | None = None) -> dict[str, dict]:
    total = _probe_total()
    if total <= 0:
        return {}
    items = _fetch_all(total)
    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(items, ensure_ascii=False, indent=2), 'utf-8')
    return _clean(items)


def _probe_total() -> int:
    body, _, _ = fetch(f'{HK_API}?k=switch&directory=software&size=1&p=1', timeout=15)
    if body:
        try:
            return json.loads(body).get('total', 0)
        except json.JSONDecodeError:
            pass
    return 0


def _fetch_all(total: int) -> list[dict]:
    if total <= 0:
        return []
    if total <= _MAX_SIZE:
        body, _, _ = fetch(
            f'{HK_API}?k=switch&directory=software&size={total}&p=1', timeout=30)
        if body is None:
            return []
        try:
            return json.loads(body).get('items', [])
        except json.JSONDecodeError:
            return []
    # 分页
    pages = (total + _MAX_SIZE - 1) // _MAX_SIZE
    items: list[dict] = []
    for p in range(1, pages + 1):
        items.extend(_pull_page(p))
    return items


def _pull_page(p: int) -> list[dict]:
    url = f'{HK_API}?k=switch&directory=software&size={_MAX_SIZE}&p={p}'
    body, _, _ = fetch(url, timeout=60)
    if body is None:
        return []
    try:
        return json.loads(body).get('items', [])
    except json.JSONDecodeError:
        return []


def _clean(items: list[dict]) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for item in items:
        nsuid = str(item.get('nsuid', ''))
        if not nsuid.isdigit() or len(nsuid) != 14:
            continue
        hw = item.get('hardwareCategory', '')
        cat = item.get('category', [])
        if hw == 'Nintendo Switch 2 Edition':
            continue
        if cat == ['體驗版']:
            continue
        if nsuid[:4] in ('7005', '7007'):
            continue
        rd = item.get('releaseDate', '')
        result[nsuid] = {
            'title': item.get('title', '') or '',
            'release_date': str(rd)[:10] if rd else '',
            'languages': ', '.join(item.get('supportedLanguages', [])) if isinstance(item.get('supportedLanguages'), list) else '',
            'publisher': item.get('publisher') or '',
            'hardware': hw,
            'category': cat,
        }
    return result
