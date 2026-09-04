"""Single-request HTTP proxy: headers and bytes always describe the same response."""
from __future__ import annotations

import mimetypes
from urllib.parse import quote

import anyio
import httpx
from fastapi import HTTPException
from starlette.responses import Response, StreamingResponse

from Backend.helper.remote_http import UnsafeRemoteURL, open_remote, public_url


class RemoteStreamingResponse(StreamingResponse):
    def __init__(self, upstream, client, headers, on_chunk, on_close):
        self.upstream = upstream
        self.client = client
        self.on_close = on_close
        self.closed = False

        async def chunks():
            async for chunk in upstream.aiter_raw(chunk_size=256 * 1024):
                if on_chunk:
                    on_chunk(len(chunk))
                yield chunk

        super().__init__(chunks(), status_code=upstream.status_code, headers=headers)

    async def close(self):
        if not self.closed:
            self.closed = True
            with anyio.CancelScope(shield=True):
                try:
                    await self.upstream.aclose()
                finally:
                    await self.client.aclose()
                    if self.on_close:
                        self.on_close()

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self.close()


async def remote_media_response(request, url, name, on_chunk=None, on_close=None):
    if not public_url(url):
        raise HTTPException(400, "Invalid or non-public source URL")
    client = httpx.AsyncClient(timeout=httpx.Timeout(90, connect=15), trust_env=False)
    upstream = None
    owned = True
    try:
        headers = {"User-Agent": "MovieBox/1.0"}
        for key in ("Range", "If-Range"):
            if request.headers.get(key):
                headers[key] = request.headers[key]
        method = "HEAD" if request.method == "HEAD" else "GET"
        upstream = await open_remote(client, method, url, headers)
        if method == "HEAD" and upstream.status_code == 405:
            await upstream.aclose()
            # Open a GET but never consume its body; avoid a one-byte probe whose
            # length would misrepresent the full resource in a HEAD response.
            upstream = await open_remote(client, "GET", url, headers)
        if upstream.status_code == 416:
            out = {"Cache-Control": "private, no-store"}
            if upstream.headers.get("content-range"):
                out["Content-Range"] = upstream.headers["content-range"]
            return Response(status_code=416, headers=out)
        if upstream.status_code not in {200, 206}:
            status = 404 if upstream.status_code in {404, 410} else 502
            raise HTTPException(status, "Remote source unavailable")
        mime = upstream.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if mime in {"text/html", "application/xhtml+xml", "application/json", "text/plain"}:
            raise HTTPException(502, "Source returned a login, quota, or error page instead of media")
        out = {"Content-Type": upstream.headers.get("content-type") or mimetypes.guess_type(name)[0] or "application/octet-stream",
               "Content-Disposition": "inline; filename*=UTF-8''" + quote(name or "video.mkv", safe=""),
               "Cache-Control": "private, no-store",
               "Access-Control-Allow-Origin": "*",
               "Access-Control-Expose-Headers": "Content-Length, Content-Range, Accept-Ranges"}
        for key in ("content-length", "content-range", "accept-ranges", "content-encoding", "etag", "last-modified"):
            if upstream.headers.get(key):
                out[key] = upstream.headers[key]
        if method == "HEAD":
            response = Response(status_code=upstream.status_code, headers=out)
            if "content-length" not in upstream.headers:
                del response.headers["content-length"]
            return response
        result = RemoteStreamingResponse(upstream, client, out, on_chunk, on_close)
        owned = False
        return result
    except UnsafeRemoteURL as exc:
        raise HTTPException(400, "Invalid or non-public source URL") from exc
    except httpx.TimeoutException as exc:
        raise HTTPException(504, "Remote source timed out") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Could not connect to remote source") from exc
    finally:
        if owned:
            with anyio.CancelScope(shield=True):
                if upstream is not None:
                    await upstream.aclose()
                await client.aclose()
