import re

# Phase 1: RSC payload 精确匹配 applications[0].id
_APP_RE = re.compile(
    r'\\"applications\\":\[{\\"id\\":\\"((?:0100|0400)[a-fA-F0-9]{12})\\"'
)
# Phase 2: 全页扫描所有 0100/0400 hex 串
_TID_RE = re.compile(r'\b(?:0100|0400)[a-fA-F0-9]{12}\b')


def extract_tid(html: str) -> str | None:
    m = _APP_RE.search(html)
    if m:
        return m.group(1).upper()
    tids = _TID_RE.findall(html)
    if not tids:
        return None
    base = [t for t in tids if t[-3:] == '000']
    return (base[0] if base else tids[0]).upper()


_BS = chr(92)
_DQ = _BS + '"'

# Nintendo BASE 页面中文语言名 → English
_CHINESE_NAME_TO_EN = {
    '日文': 'Japanese', '英文': 'English', '西班牙文': 'Spanish',
    '法文': 'French', '葡萄牙文': 'Portuguese', '德文': 'German',
    '意大利文': 'Italian', '荷蘭文': 'Dutch', '俄文': 'Russian',
    '韓文': 'Korean', '泰文': 'Thai', '波蘭文': 'Polish',
    '中文 (簡體字)': 'Simplified Chinese', '中文 (繁體字)': 'Traditional Chinese',
    '中文 (简体字)': 'Simplified Chinese', '中文 (繁体字)': 'Traditional Chinese',
}


# ISO→English（titledb 用 ISO code，需映射）
_ISO_TO_EN = {
    'ja': 'Japanese', 'en': 'English', 'fr': 'French', 'de': 'German',
    'it': 'Italian', 'es': 'Spanish', 'pt': 'Portuguese', 'nl': 'Dutch',
    'ru': 'Russian', 'ko': 'Korean', 'zh': 'Chinese',
    'th': 'Thai', 'pl': 'Polish',
}


def iso_to_english(codes: list[str]) -> str:
    """['ja', 'zh', 'zh'] -> 'Japanese, Simplified Chinese, Traditional Chinese'。
    重复 zh 时拆分为简/繁，其余去重。"""
    seen = set()
    result = []
    zh_count = 0
    for c in codes:
        if c == 'zh':
            zh_count += 1
            continue
        if c not in seen:
            seen.add(c)
            result.append(_ISO_TO_EN.get(c, c))
    if zh_count >= 2:
        result.extend(['Simplified Chinese', 'Traditional Chinese'])
    elif zh_count == 1:
        result.append('Chinese')
    return ', '.join(result)


def extract_base_page(html: str) -> dict | None:
    """从 BASE /titles/ 页面 RSC payload 提取 tid, name, date, languages, publisher。"""
    # 定位 nsUid 块
    fm = html.find(_DQ + 'formalName' + _DQ + ':' + _DQ)
    if fm < 0:
        return None

    result: dict = {}

    # 名称
    s = fm + len(_DQ + 'formalName' + _DQ + ':' + _DQ)
    e = html.find(_DQ, s)
    result['name'] = html[s:e] if e > 0 else ''

    # TID: applications":[{"id":"0100...
    apps = html.find('applications' + _DQ + ':[{' + _DQ + 'id' + _DQ + ':' + _DQ)
    if apps >= 0:
        s = apps + len('applications' + _DQ + ':[{' + _DQ + 'id' + _DQ + ':' + _DQ)
        e = html.find(_DQ, s)
        result['tid'] = html[s:e].upper() if e > 0 else None
    else:
        # 降级: 全页扫描（旧 Phase 2）
        result['tid'] = extract_tid(html)

    # 发售日
    rd = html.find('releaseDateOnEshop' + _DQ + ':' + _DQ)
    if rd >= 0:
        s = rd + len('releaseDateOnEshop' + _DQ + ':' + _DQ)
        e = html.find(_DQ, s)
        result['date'] = html[s:e] if e > 0 else ''

    # 发行商
    pub = html.find(_DQ + 'publisher' + _DQ + ':{' + _DQ + 'name' + _DQ + ':' + _DQ)
    if pub >= 0:
        s = pub + len(_DQ + 'publisher' + _DQ + ':{' + _DQ + 'name' + _DQ + ':' + _DQ)
        e = html.find(_DQ, s)
        result['publisher'] = html[s:e] if e > 0 else ''

    # 语言: 限定 languages\":[...] 数组内
    lstart = html.find('languages' + _DQ + ':[')
    if lstart >= 0:
        arr_start = lstart + len('languages' + _DQ + ':')
        # JSON 数组括号配对
        depth, end = 0, arr_start
        for i in range(arr_start, min(arr_start + 5000, len(html))):
            if html[i] == '[': depth += 1
            elif html[i] == ']':
                depth -= 1
                if depth <= 0: end = i + 1; break
        chunk = html[arr_start:end]
        names = re.findall(r'\\"name\\":\\"([^\\]+)\\"', chunk)
        result['languages'] = [_CHINESE_NAME_TO_EN.get(n, n) for n in names]

    return result if result.get('name') else None


def derive_parent_tid(dlc_tid: str) -> str:
    if len(dlc_tid) != 16 or dlc_tid[-3:] == '000':
        return ''
    try:
        b = int(dlc_tid[-4], 16)
        if b == 0:
            return ''
        return dlc_tid[:-4] + hex(b - 1)[2:].upper() + '000'
    except ValueError:
        return ''
