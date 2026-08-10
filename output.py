import json
from pathlib import Path


def write(
    matched: dict[str, list],
    unmatched: list[dict],
    output_dir: Path,
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)

    def _sort_key(item):
        nsuid, entry = item
        d = (entry[2] or '') if len(entry) > 2 else ''
        d = d.strip()
        if len(d) == 8 and d.isdigit():
            d = f'{d[:4]}-{d[4:6]}-{d[6:8]}'
        return (d or '9999-99-99', int(nsuid))

    result = dict(sorted(matched.items(), key=_sort_key))
    map_path = output_dir / 'hk_tid_map.json'
    map_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), 'utf-8')

    unmatched.sort(key=lambda x: int(x['nsuid']))
    unmatch_path = output_dir / 'hk_tid_map_unmatched.json'
    unmatch_path.write_text(json.dumps(unmatched, ensure_ascii=False, indent=2), 'utf-8')

    return map_path, unmatch_path
