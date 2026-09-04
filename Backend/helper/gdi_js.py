"""GDI-JS JSON adapter. Credentials and signed links never become media IDs."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from http.cookies import CookieError, SimpleCookie
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlsplit

import httpx

from Backend.helper.remote_http import public_url, resolve_public_address

MAX_PAGE_BYTES = 4 * 1024 * 1024
MAX_PAGES = 1000
MAX_FILES = 50000
FOLDER_MIME = "application/vnd.google-apps.folder"


class GDIError(ValueError):
    """Only safe, user-facing messages; never include response bodies or secrets."""


def canonical_path(value: str, *, folder: bool = False) -> str:
    if not isinstance(value, str) or not re.match(r"^/\d+:/", value):
        raise GDIError("Use a GDI-JS path such as /0:/Movies/.")
    if "?" in value or "#" in value or len(value) > 4096:
        raise GDIError("Invalid GDI-JS path.")
    decoded = unquote(value, errors="strict")
    probe = decoded
    for _ in range(5):
        if "\\" in probe or any(ord(c) < 32 or ord(c) == 127 for c in probe):
            raise GDIError("Unsafe GDI-JS path.")
        if any(p in {".", ".."} for p in probe.split("/")) or "//" in probe:
            raise GDIError("Relative path traversal is not allowed.")
        if re.search(r"%(?:2f|5c)", probe, re.I):
            raise GDIError("Encoded path separators are not allowed.")
        next_probe = unquote(probe, errors="strict")
        if next_probe == probe:
            break
        probe = next_probe
    else:
        raise GDIError("Excessively encoded GDI-JS path.")
    # Reject separators hidden in the first encoding layer as well.
    if re.search(r"%(?:2f|5c)", value, re.I):
        raise GDIError("Encoded path separators are not allowed.")
    result = quote(decoded, safe="/:")
    return result.rstrip("/") + "/" if folder else result


@dataclass(frozen=True)
class GDIConfig:
    root_url: str
    username: str = ""
    password: str = field(default="", repr=False)

    def __post_init__(self):
        parsed = urlsplit(self.root_url)
        if not public_url(self.root_url) or parsed.scheme != "https" or parsed.query or parsed.fragment:
            raise GDIError("GDI-JS requires an HTTPS index URL without a query, credentials or fragment.")
        path = canonical_path(parsed.path, folder=True)
        origin = str(httpx.URL(self.root_url).copy_with(path="/", query=None, fragment=None)).rstrip("/")
        object.__setattr__(self, "root_url", origin + path)

    @property
    def origin(self):
        p = urlsplit(self.root_url)
        return f"{p.scheme}://{p.netloc}"

    @property
    def root_path(self):
        return urlsplit(self.root_url).path

    def path(self, value, *, folder=False):
        result = canonical_path(value, folder=folder)
        if not result.startswith(self.root_path):
            raise GDIError("Folder/file is outside the configured index root.")
        return result

    def selections(self, paths):
        if not isinstance(paths, list) or len(paths) > 100:
            raise GDIError("Select up to 100 folders.")
        normalized = sorted({self.path(p, folder=True) for p in paths}, key=lambda p: (len(p), p))
        result = []
        for path in normalized:
            if not any(path.startswith(parent) for parent in result):
                result.append(path)
        return result


def config_from_settings(settings):
    return GDIConfig(settings.gdrive_index_url, settings.gdrive_index_username, settings.gdrive_index_password)


def validate_settings_update(current, payload):
    """Validate in-place without networking; blank password means keep stored value."""
    clear = payload.pop("gdrive_clear_password", False)
    if type(clear) is not bool:
        raise GDIError("Invalid clear-password value.")
    if "gdrive_index_password" in payload:
        if not isinstance(payload["gdrive_index_password"], str) or len(payload["gdrive_index_password"]) > 4096:
            raise GDIError("Invalid GDI-JS password.")
        if payload["gdrive_index_password"] == "":
            del payload["gdrive_index_password"]
    if clear:
        payload["gdrive_index_password"] = ""
    for key in ("gdrive_index_url", "gdrive_index_username", "gdrive_source_type"):
        if key in payload:
            if not isinstance(payload[key], str):
                raise GDIError("Invalid GDI-JS setting type.")
            payload[key] = payload[key].strip()
    merged = {**current, **payload}
    if merged.get("gdrive_source_type", "html") not in {"html", "gdi_js"}:
        raise GDIError("Source type must be html or gdi_js.")
    old_url = current.get("gdrive_index_url") or ""
    new_url = merged.get("gdrive_index_url") or ""
    if new_url != old_url and "gdrive_selected_folders" not in payload:
        payload["gdrive_selected_folders"] = []
    if current.get("gdrive_index_password") and (urlsplit(old_url).netloc.lower(), urlsplit(old_url).scheme) != (urlsplit(new_url).netloc.lower(), urlsplit(new_url).scheme):
        if "gdrive_index_password" not in payload:
            raise GDIError("Changing index origin requires re-entering its password or selecting Clear saved index password.")
    if merged.get("gdrive_source_type") == "gdi_js":
        config = GDIConfig(new_url)
        payload["gdrive_index_url"] = config.root_url
        if "gdrive_selected_folders" in payload:
            payload["gdrive_selected_folders"] = config.selections(payload["gdrive_selected_folders"])
    elif "gdrive_selected_folders" in payload and payload["gdrive_selected_folders"]:
        raise GDIError("Folder selections require the GDI-JS source type.")


async def open_scoped(client, method, url, origin, headers=None, content=None):
    """Single HTTPS hop, DNS-pinned. Never forward credentials through redirects."""
    if not public_url(url):
        raise GDIError("GDI-JS source must use a public address.")
    original = httpx.URL(url)
    expected = httpx.URL(origin)
    if original.scheme != "https" or (original.scheme, original.host, original.port) != (expected.scheme, expected.host, expected.port):
        raise GDIError("GDI-JS requests cannot leave the configured HTTPS origin.")
    address = await resolve_public_address(original.host, original.port or 443)
    allowed = {"cookie", "content-type", "range", "if-range", "user-agent"}
    outgoing = {k: v for k, v in (headers or {}).items() if k.lower() in allowed}
    outgoing.update({"Host": original.netloc.decode("ascii"), "Accept-Encoding": "identity", "Connection": "close"})
    request = client.build_request(method, original.copy_with(host=address), headers=outgoing, content=content,
                                   extensions={"sni_hostname": original.host})
    # httpx may have collected cookies against a pinned IP; never use those.
    request.headers.pop("cookie", None)
    request.headers.pop("authorization", None)
    if (headers or {}).get("Cookie"):
        request.headers["Cookie"] = headers["Cookie"]
    response = await client.send(request, stream=True, follow_redirects=False)
    if 300 <= response.status_code < 400:
        await response.aclose()
        raise GDIError("GDI-JS redirected the request. Use the final index URL; login/Cloudflare redirects are not supported.")
    return response


@dataclass
class _Session:
    cookie: str = field(default="", repr=False)
    expires: float = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_sessions: OrderedDict[str, _Session] = OrderedDict()


def _session(config):
    key = hashlib.sha256(json.dumps([config.origin, config.username, config.password]).encode()).hexdigest()
    if key not in _sessions:
        # Bounded process-memory cache. Cookies never enter MongoDB or API output.
        if len(_sessions) >= 8:
            _sessions.popitem(last=False)
        _sessions[key] = _Session()
    _sessions.move_to_end(key)
    return _sessions[key]


class GDIClient:
    def __init__(self, config):
        self.config = config
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(30, connect=15), trust_env=False)
        self.session = _session(config)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.client.aclose()

    def cookie(self):
        return self.session.cookie if self.session.expires > time.monotonic() else ""

    async def _read(self, path, body, *, login=False):
        try:
            async with asyncio.timeout(35):
                return await self._read_bounded(path, body, login=login)
        except TimeoutError:
            raise GDIError("GDI-JS API timed out. Try a smaller folder or check worker availability.") from None

    async def _read_bounded(self, path, body, *, login=False):
        headers = {"Content-Type": "application/x-www-form-urlencoded" if login else "application/json"}
        if not login and self.cookie():
            headers["Cookie"] = self.cookie()
        try:
            response = await open_scoped(self.client, "POST", self.config.origin + path,
                                         self.config.origin, headers, body)
            try:
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise GDIError("Compressed GDI-JS API responses are not supported.")
                raw = bytearray()
                async for chunk in response.aiter_raw(chunk_size=64 * 1024):
                    if len(raw) + len(chunk) > MAX_PAGE_BYTES:
                        raise GDIError("GDI-JS API page exceeded the 4 MiB safety limit.")
                    raw.extend(chunk)
                try:
                    data = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    data = None
                return response.status_code, data, response.headers.get_list("set-cookie")
            finally:
                await response.aclose()
        except httpx.HTTPError:
            raise GDIError("Could not reach the GDI-JS API. Check the URL and worker availability.") from None

    async def _login(self, previous_cookie):
        if not self.config.username or not self.config.password:
            raise GDIError("Index requires login. Save its username and password under Settings → Google Drive source.")
        async with self.session.lock:
            if self.cookie() and self.cookie() != previous_cookie:
                return
            self.session.cookie = ""
            self.session.expires = 0
            body = urlencode({"username": self.config.username, "password": self.config.password}).encode()
            status, data, cookies = await self._read("/login", body, login=True)
            if status != 200 or not isinstance(data, dict) or data.get("ok") is not True:
                raise GDIError("GDI-JS login failed. Check the saved index credentials; never use your MovieBox password unless they are the same.")
            cookie = SimpleCookie()
            try:
                for value in cookies:
                    cookie.load(value)
            except CookieError:
                raise GDIError("GDI-JS returned an invalid login session.") from None
            if "session" not in cookie or not cookie["session"].value:
                raise GDIError("GDI-JS login returned no session cookie.")
            self.session.cookie = "session=" + cookie["session"].value
            self.session.expires = time.monotonic() + 600

    async def json(self, path, payload):
        body = json.dumps(payload).encode()
        previous_cookie = self.cookie()
        status, data, _ = await self._read(path, body)
        if status == 401 or (status == 200 and data is None):
            await self._login(previous_cookie)
            status, data, _ = await self._read(path, body)
        if status in {401, 403}:
            raise GDIError("GDI-JS access denied. Check login permissions, folder passwords and worker protection settings.")
        if status == 404:
            raise GDIError("GDI-JS folder/file was not found. Browse and select it again.")
        if status == 429:
            raise GDIError("GDI-JS rate limit reached. Wait before retrying.")
        if status != 200 or not isinstance(data, dict) or data.get("error") or data.get("ok") is False:
            raise GDIError("GDI-JS returned an invalid listing. Check the worker API and folder permissions.")
        return data

    async def list_page(self, path, page_token=None, page_index=0):
        path = self.config.path(path, folder=True)
        if type(page_index) is not int or not 0 <= page_index < MAX_PAGES:
            raise GDIError("Invalid GDI-JS page number.")
        if page_token is not None and (not isinstance(page_token, str) or len(page_token) > 8192):
            raise GDIError("Invalid GDI-JS page token.")
        data = await self.json(path, {"page_token": page_token, "page_index": page_index})
        files = (data.get("data") or {}).get("files") if isinstance(data.get("data"), dict) else None
        token = data.get("nextPageToken") or None
        if not isinstance(files, list) or len(files) > 10000 or (token is not None and not isinstance(token, str)):
            raise GDIError("GDI-JS returned an unsupported folder response.")
        items = []
        for item in files:
            if not isinstance(item, dict):
                raise GDIError("GDI-JS returned an invalid file entry.")
            name = item.get("name")
            if not isinstance(name, str) or not name or "/" in name or "\\" in name or name in {".", ".."}:
                raise GDIError("GDI-JS returned an unsafe file/folder name.")
            is_folder = item.get("mimeType") == FOLDER_MIME
            child = self.config.path(path + quote(name, safe=""), folder=is_folder)
            try:
                size = max(0, int(item.get("size") or 0))
            except (ValueError, TypeError):
                size = 0
            # Deliberately discard encrypted IDs and expiring signed links.
            items.append({"name": name, "path": child, "is_folder": is_folder, "size_bytes": size})
        return {"path": path, "items": items, "next_page_token": token, "page_index": page_index}

    async def resolve_file(self, url):
        parsed = urlsplit(url)
        if f"{parsed.scheme}://{parsed.netloc}" != self.config.origin or parsed.query or parsed.fragment:
            raise GDIError("Media source no longer matches the configured GDI-JS index.")
        path = self.config.path(parsed.path)
        if path.endswith("/"):
            raise GDIError("A folder cannot be played as a video.")
        data = await self.json(path, {})
        link = data.get("link")
        if not isinstance(link, str) or len(link) > 16384:
            raise GDIError("GDI-JS returned no usable playback link.")
        resolved = urljoin(self.config.origin + "/", link)
        target = urlsplit(resolved)
        query = parse_qs(target.query)
        if (f"{target.scheme}://{target.netloc}" != self.config.origin or target.path != "/download.aspx"
                or target.fragment or not all(query.get(k) for k in ("file", "expiry", "mac"))):
            raise GDIError("GDI-JS playback link left the expected download endpoint.")
        return resolved


async def discover(config, folders, includes=None, excludes=None, cancelled=None, progress=None):
    from Backend.helper.gdrive_source import _apply_filters, is_video_filename, parse_filter_tokens
    selected = config.selections(folders)
    if not selected:
        raise GDIError("Choose and save at least one folder in Tools before scanning.")
    includes, excludes = parse_filter_tokens(includes), parse_filter_tokens(excludes)
    queue, visited, seen, files = deque(selected), set(), set(), []
    pages = 0
    async with GDIClient(config) as client:
        while queue:
            path = queue.popleft()
            if path in visited:
                continue
            visited.add(path)
            token, index, tokens = None, 0, set()
            while True:
                if cancelled and cancelled():
                    return []  # Cancelled discovery must not index a partial snapshot.
                if pages >= MAX_PAGES:
                    raise GDIError("Scan reached 1,000 API pages. Select smaller folders and scan again.")
                page = await client.list_page(path, token, index)
                pages += 1
                for item in page["items"]:
                    text = unquote(item["path"])
                    if not _apply_filters(text, [], excludes):
                        continue
                    if item["is_folder"]:
                        if item["path"] not in visited:
                            queue.append(item["path"])
                        if len(queue) + len(visited) > MAX_FILES:
                            raise GDIError("Too many folders. Select a smaller subtree.")
                    elif is_video_filename(item["name"]) and _apply_filters(text, includes, excludes) and item["path"] not in seen:
                        seen.add(item["path"])
                        files.append({**item, "url": config.origin + item["path"], "kind": "gdi_js"})
                        if len(files) > MAX_FILES:
                            raise GDIError("Scan exceeded 50,000 videos. Select smaller folders.")
                if progress:
                    progress(pages, len(files), path)
                token = page["next_page_token"]
                if not token:
                    break
                if token in tokens:
                    raise GDIError("GDI-JS repeated a page token; scan stopped to avoid a loop.")
                tokens.add(token)
                index += 1
    return files


def media_opener(settings):
    config = config_from_settings(settings)

    async def opener(client, method, url, headers):
        async with GDIClient(config) as gdi:
            fresh_url = await gdi.resolve_file(url)
            outgoing = dict(headers)
            if gdi.cookie():
                outgoing["Cookie"] = gdi.cookie()
            return await open_scoped(client, method, fresh_url, config.origin, outgoing)
    return opener
