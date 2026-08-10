"""HTTP 工具: 统一 fetch/重试/ETag/代理。"""
import urllib.request
import urllib.error
import time
import uuid
from pathlib import Path

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'


def _read_windows_proxy() -> str | None:
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r'Software\Microsoft\Windows\CurrentVersion\Internet Settings',
        )
        enabled, _ = winreg.QueryValueEx(key, 'ProxyEnable')
        if not enabled:
            winreg.CloseKey(key)
            return None
        server, _ = winreg.QueryValueEx(key, 'ProxyServer')
        winreg.CloseKey(key)
        if not server:
            return None
        server = server.replace(' ', '')
        if '=' in server:
            for part in server.split(';'):
                if '=' in part:
                    _, val = part.split('=', 1)
                    if val:
                        return f'http://{val}'
        return f'http://{server}'
    except Exception:
        return None


def setup_proxy() -> str | None:
    """安装 Windows 系统代理（如果启用）。CI 无代理时返回 None。"""
    proxy = _read_windows_proxy()
    if proxy:
        handler = urllib.request.ProxyHandler({'http': proxy, 'https': proxy})
        urllib.request.install_opener(urllib.request.build_opener(handler))
    return proxy


def fetch(url: str, retries: int = 3, timeout: int = 30,
          headers: dict | None = None) -> tuple[bytes | None, int, dict]:
    """HTTP GET，失败自动重试。-> (body, status_code, response_headers)。"""
    hdrs = {'User-Agent': UA}
    if headers:
        hdrs.update(headers)
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read(), resp.status, dict(resp.headers)
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return None, 304, dict(e.headers)
            if e.code == 404:
                return None, 404, dict(e.headers)
        except (urllib.error.URLError, OSError, TimeoutError):
            pass
        if attempt < retries:
            time.sleep(attempt * 0.5)
    return None, 0, {}


def download_with_etag(url: str, dest: Path) -> bool:
    """ETag 条件下载，304 时跳过传输。原子写入防损坏。返回 True=数据可用。"""
    etag_file = dest.parent / f'{dest.name}.etag'
    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.exists() and etag_file.exists():
        etag = etag_file.read_text().strip()
        _, status, _ = fetch(url, headers={'If-None-Match': etag})
        if status == 304:
            dest.touch()
            return True

    body, _, resp_headers = fetch(url, timeout=120)
    if body is None:
        return dest.exists()

    tmp = dest.parent / f'{dest.name}.{uuid.uuid4().hex[:8]}.tmp'
    tmp.write_bytes(body)
    tmp.replace(dest)

    new_etag = resp_headers.get('ETag') or resp_headers.get('etag')
    if new_etag:
        etag_file.write_text(new_etag)
    elif etag_file.exists():
        etag_file.unlink()

    return True
