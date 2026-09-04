from __future__ import annotations

import ipaddress
import re
from html import unescape
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import httpx

VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".avi", ".mov", ".m4v", ".webm", ".ts", ".m2ts", ".wmv", ".flv",
}

_A_HREF_RE = re.compile(r"""<a[^>]+href=["']([^"']+)["'][^>]*>(.*?)</a>""", re.IGNORECASE | re.DOTALL)
_DRIVE_FOLDER_ID_RE = re.compile(r"/drive/folders/([a-zA-Z0-9_-]+)")
_DRIVE_FILE_ID_RE = re.compile(r"/file/d/([a-zA-Z0-9_-]+)")


def parse_filter_tokens(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        raw = "\n".join(str(v) for v in value if str(v).strip())
    else:
        raw = str(value)
    tokens: list[str] = []
    for part in raw.replace(",", "\n").splitlines():
        t = part.strip().lower()
        if t:
            tokens.append(t)
    return tokens


def is_video_filename(name: str) -> bool:
    lower = (name or "").lower().strip()
    return any(lower.endswith(ext) for ext in VIDEO_EXTENSIONS)


def _is_private_host(hostname: str) -> bool:
    if not hostname:
        return True
    h = hostname.lower().strip("[]")
    if h in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast)


def is_safe_remote_url(url: str) -> bool:
    try:
        p = urlparse(str(url).strip())
    except Exception:
        return False
    if p.scheme not in ("http", "https"):
        return False
    if not p.netloc:
        return False
    return not _is_private_host(p.hostname or "")


def _extract_name_from_href(href: str) -> str:
    path = (urlparse(href).path or "").rstrip("/")
    if not path:
        return ""
    return path.rsplit("/", 1)[-1]


def _apply_filters(path_text: str, includes: list[str], excludes: list[str]) -> bool:
    target = (path_text or "").lower()
    if excludes and any(x in target for x in excludes):
        return False
    if includes and not any(x in target for x in includes):
        return False
    return True


def normalize_drive_download_url(url: str) -> str:
    p = urlparse(url)
    qs = parse_qs(p.query or "")
    file_id = None
    m = _DRIVE_FILE_ID_RE.search(p.path or "")
    if m:
        file_id = m.group(1)
    elif "id" in qs and qs["id"]:
        file_id = qs["id"][0]
    if file_id:
        return f"https://drive.google.com/uc?export=download&id={file_id}"
    return url


def extract_drive_folder_id(url: str) -> str | None:
    p = urlparse(url or "")
    m = _DRIVE_FOLDER_ID_RE.search(p.path or "")
    if m:
        return m.group(1)
    qs = parse_qs(p.query or "")
    if "id" in qs and qs["id"]:
        return qs["id"][0]
    return None


async def _fetch_text(client: httpx.AsyncClient, url: str) -> str:
    resp = await client.get(url)
    resp.raise_for_status()
    return resp.text or ""


async def _crawl_index(
    client: httpx.AsyncClient,
    root_url: str,
    include_filters: list[str],
    exclude_filters: list[str],
    max_pages: int = 120,
) -> list[dict]:
    files: list[dict] = []
    queue = [root_url]
    visited: set[str] = set()
    root_host = urlparse(root_url).netloc.lower()

    while queue and len(visited) < max_pages:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        try:
            html = await _fetch_text(client, url)
        except Exception:
            continue

        for href_raw, label_html in _A_HREF_RE.findall(html):
            href_raw = unescape(href_raw or "").strip()
            if not href_raw or href_raw.startswith(("javascript:", "mailto:", "#")):
                continue
            full = urljoin(url, href_raw)
            if not is_safe_remote_url(full):
                continue
            name = unescape(re.sub(r"<[^>]*>", "", label_html or "")).strip() or _extract_name_from_href(full)
            path_text = f"{full} {name}"
            if not _apply_filters(path_text, include_filters, exclude_filters):
                continue

            parsed = urlparse(full)
            if parsed.netloc.lower() == root_host and (href_raw.endswith("/") or parsed.path.endswith("/")):
                if full not in visited:
                    queue.append(full)
                continue

            normalized = normalize_drive_download_url(full)
            if is_video_filename(name):
                files.append({"name": name, "url": normalized, "path": full, "size_bytes": 0})
    return files


async def _list_drive_folder_embedded(
    client: httpx.AsyncClient,
    folder_id: str,
    include_filters: list[str],
    exclude_filters: list[str],
) -> list[dict]:
    files: list[dict] = []
    html = await _fetch_text(client, f"https://drive.google.com/embeddedfolderview?id={folder_id}#list")
    for href_raw, label_html in _A_HREF_RE.findall(html):
        href_raw = unescape(href_raw or "").strip()
        if not href_raw:
            continue
        full = urljoin("https://drive.google.com/", href_raw)
        m = _DRIVE_FILE_ID_RE.search(urlparse(full).path or "")
        if not m:
            continue
        file_id = m.group(1)
        name = unescape(re.sub(r"<[^>]*>", "", label_html or "")).strip() or file_id
        path_text = f"{name} {full}"
        if not _apply_filters(path_text, include_filters, exclude_filters):
            continue
        if is_video_filename(name):
            files.append({
                "name": name,
                "url": f"https://drive.google.com/uc?export=download&id={file_id}",
                "path": full,
                "size_bytes": 0,
            })
    return files


async def discover_gdrive_files(
    *,
    index_url: str = "",
    folder_url: str = "",
    include_filters: Any = None,
    exclude_filters: Any = None,
) -> list[dict]:
    include = parse_filter_tokens(include_filters)
    exclude = parse_filter_tokens(exclude_filters)
    discovered: list[dict] = []
    seen = set()

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        idx = (index_url or "").strip()
        if idx and is_safe_remote_url(idx):
            for item in await _crawl_index(client, idx, include, exclude):
                key = item.get("url")
                if key and key not in seen:
                    seen.add(key)
                    discovered.append(item)

        folder = (folder_url or "").strip()
        folder_id = extract_drive_folder_id(folder) if folder else None
        if folder_id:
            try:
                items = await _list_drive_folder_embedded(client, folder_id, include, exclude)
            except Exception:
                items = []
            for item in items:
                key = item.get("url")
                if key and key not in seen:
                    seen.add(key)
                    discovered.append(item)
    return discovered


async def check_remote_stream_alive(url: str) -> bool | None:
    if not is_safe_remote_url(url):
        return False
    headers = {"Range": "bytes=0-0", "User-Agent": "MovieBox/1.0"}
    try:
        async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code in (200, 206):
                return True
            if resp.status_code in (401, 403):
                return None
            if resp.status_code == 405:
                h = await client.head(url)
                return h.status_code in (200, 206)
            return False
    except Exception:
        return None
