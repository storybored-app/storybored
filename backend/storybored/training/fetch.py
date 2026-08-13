# OWNED-BY: training-agent
"""Dataset image importer: download URLs into a character's staging dir.

Guards: 10MB per file, image/* content-type, max 60 files, request timeouts,
sane file extensions. Downloads land in DATA_DIR/training/{handle}/raw next to
multipart uploads staged by the wizard endpoint.
"""

import ipaddress
import shutil
import socket
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from storybored.config import Settings

MAX_FILES = 60
MAX_BYTES = 10 * 1024 * 1024  # 10MB per image
CHUNK = 64 * 1024
MAX_REDIRECTS = 5
DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

#: Escape hatch for tests that download from a local (loopback) fixture server.
#: NEVER enabled in production — the SSRF guard below stays on by default.
ALLOW_PRIVATE_HOSTS = False

CONTENT_TYPE_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/gif": ".gif",
    "image/tiff": ".tiff",
}
ALLOWED_EXTS = set(CONTENT_TYPE_EXT.values()) | {".jpeg"}


def staging_dir(settings: Settings, handle: str, *, clean: bool = False) -> Path:
    """DATA_DIR/training/{handle}/raw — created (optionally emptied) on demand."""
    path = settings.data_path / "training" / handle / "raw"
    if clean and path.is_dir():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def ext_for(url: str, content_type: str) -> str | None:
    """Pick a sane extension from the content-type, falling back to the URL."""
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in CONTENT_TYPE_EXT:
        return CONTENT_TYPE_EXT[ct]
    suffix = Path(httpx.URL(url).path).suffix.lower()
    if suffix in ALLOWED_EXTS:
        return ".jpg" if suffix == ".jpeg" else suffix
    return None


def _blocked_ip(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for any address we must never fetch (SSRF-sensitive ranges)."""
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def _check_url(url: str) -> str | None:
    """Return an error string if `url` is unsafe to fetch, else None.

    Blocks non-http(s) schemes and any host that resolves to a private,
    loopback, link-local (incl. 169.254.169.254 cloud metadata), reserved,
    multicast or unspecified address — v4 and v6."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return "only http(s) URLs are supported"
    host = parts.hostname
    if not host:
        return "URL has no host"
    if ALLOW_PRIVATE_HOSTS:
        return None
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        return f"could not resolve host: {exc}"
    if not infos:
        return "could not resolve host"
    for info in infos:
        ip_str = info[4][0].split("%")[0]  # strip any scope id
        try:
            addr = ipaddress.ip_address(ip_str)
        except ValueError:
            return "host resolved to an invalid address"
        if _blocked_ip(addr):
            return "refusing to fetch a private/loopback address"
    return None


def fetch_images(
    urls: list[str],
    dest: Path,
    *,
    start_index: int = 0,
    timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
) -> list[dict]:
    """Download each URL into dest as url_{i:03d}{ext}.

    Returns one result dict per URL: {"url", "ok", "path"|"error", "bytes"?}.
    Individual failures never raise — only a >MAX_FILES batch does.
    """
    if len(urls) > MAX_FILES:
        raise ValueError(f"too many URLs (max {MAX_FILES})")
    dest.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    # Redirects are followed manually so every hop's resolved host is
    # re-validated against the SSRF guard (auto-follow could bounce to an
    # internal target after a safe-looking first URL).
    with httpx.Client(follow_redirects=False, timeout=timeout) as client:
        for i, url in enumerate(urls):
            results.append(_fetch_one(client, url, dest, start_index + i))
    return results


def _fetch_one(client: httpx.Client, url: str, dest: Path, index: int) -> dict:
    original = url

    def fail(error: str) -> dict:
        return {"url": original, "ok": False, "error": error}

    current = url
    try:
        for _hop in range(MAX_REDIRECTS + 1):
            err = _check_url(current)
            if err:
                return fail(err)
            with client.stream("GET", current) as resp:
                if resp.is_redirect:
                    location = resp.headers.get("location")
                    if not location:
                        return fail(f"HTTP {resp.status_code} without redirect target")
                    current = urljoin(current, location)
                    continue
                if resp.status_code != 200:
                    return fail(f"HTTP {resp.status_code}")
                content_type = resp.headers.get("content-type", "")
                if not content_type.split(";")[0].strip().lower().startswith("image/"):
                    return fail(f"not an image (content-type: {content_type or 'missing'})")
                length = resp.headers.get("content-length")
                if length and length.isdigit() and int(length) > MAX_BYTES:
                    return fail(f"too large ({int(length)} bytes, max {MAX_BYTES})")
                ext = ext_for(current, content_type)
                if ext is None:
                    return fail(f"unsupported image type: {content_type}")

                path = dest / f"url_{index:03d}{ext}"
                size = 0
                try:
                    with path.open("wb") as fh:
                        for chunk in resp.iter_bytes(CHUNK):
                            size += len(chunk)
                            if size > MAX_BYTES:
                                raise _TooLarge()
                            fh.write(chunk)
                except _TooLarge:
                    path.unlink(missing_ok=True)
                    return fail(f"too large (over {MAX_BYTES} bytes)")
                if size == 0:
                    path.unlink(missing_ok=True)
                    return fail("empty response body")
                return {"url": original, "ok": True, "path": str(path), "bytes": size}
        return fail(f"too many redirects (max {MAX_REDIRECTS})")
    except httpx.HTTPError as exc:
        return fail(f"download failed: {exc}")


class _TooLarge(Exception):
    pass
