"""Offline fixtures matching the uploaded GDI-JS worker's API (no real secrets)."""
import asyncio
import json
import os
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, Mock, patch

os.environ.update(API_ID="12345", API_HASH="0" * 32, BOT_TOKEN="12345:dummy",
                  OWNER_ID="12345", DATABASE="mongodb://localhost:27017,mongodb://localhost:27017")

import httpx
from fastapi import HTTPException
from starlette.requests import Request
from Backend.helper import gdi_js as gdi, remote_stream
from Backend.helper.settings_manager import Settings, SettingsManager
from Backend.helper.scan_manager import GDriveScanManager

ROOT = "https://index.example/0:/"
REAL_CLIENT = httpx.AsyncClient


class Body(httpx.AsyncByteStream):
    def __init__(self, content):
        self.content, self.closed, self.reads = content, False, 0
    async def __aiter__(self):
        self.reads += 1
        yield self.content
    async def aclose(self):
        self.closed = True


def reply(data, status=200, headers=None):
    content = data if isinstance(data, bytes) else json.dumps(data).encode()
    body = Body(content)
    return httpx.Response(status, stream=body, headers=headers or {"Content-Type": "application/json"}, extensions={"test_body": body})


def listing(items=(), token=None):
    return {"data": {"files": list(items)}, "nextPageToken": token, "curPageIndex": 0}


def item(name, folder=False):
    return {"name": name, "mimeType": gdi.FOLDER_MIME if folder else "video/mp4", "size": "6",
            "id": "encrypted-id", "link": "/download.aspx?file=OLD&expiry=OLD&mac=OLD"}


class Paths(TestCase):
    def test_root_and_encoding(self):
        config = gdi.GDIConfig(ROOT + "Anime Shows/")
        self.assertEqual(config.root_url, ROOT + "Anime%20Shows/")
        self.assertEqual(config.origin, "https://index.example")
        self.assertEqual(config.path("/0:/Anime%20Shows/Test%20S01E01.mkv"), "/0:/Anime%20Shows/Test%20S01E01.mkv")

    def test_invalid_roots(self):
        for root in ("http://index.example/0:/", "https://user:pass@index.example/0:/", ROOT + "?secret=x",
                     "https://127.0.0.1/0:/", "https://index.example/", ROOT + "#fragment"):
            with self.subTest(root=root), self.assertRaises(ValueError):
                gdi.GDIConfig(root)

    def test_scope_and_traversal(self):
        config = gdi.GDIConfig(ROOT + "Movies/")
        for path in ("/1:/Movies/", "/0:/Movies2/", "/0:/Movies/../Other/", "/0:/Movies/%2e%2e/",
                     "/0:/Movies/%252e%252e/", "/0:/Movies/a%2fb/", "/0:/Movies/%255c/", "/0:/Movies/\x00/"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                config.path(path, folder=True)

    def test_selections_deduplicate_parent_and_child(self):
        config = gdi.GDIConfig(ROOT)
        self.assertEqual(config.selections(["/0:/Movies/Sub/", "/0:/Movies/", "/0:/Movies/"]), ["/0:/Movies/"])
        with self.assertRaises(ValueError):
            config.selections("/0:/")

    def test_password_blank_keep_and_origin_change(self):
        current = {"gdrive_source_type": "gdi_js", "gdrive_index_url": ROOT, "gdrive_index_password": "fake-secret"}
        payload = {"gdrive_index_password": ""}
        gdi.validate_settings_update(current, payload)
        self.assertNotIn("gdrive_index_password", payload)
        with self.assertRaises(gdi.GDIError):
            gdi.validate_settings_update(current, {"gdrive_index_url": "https://different.example/0:/"})
        payload = {"gdrive_index_url": "https://different.example/0:/", "gdrive_clear_password": True}
        gdi.validate_settings_update(current, payload)
        self.assertEqual(payload["gdrive_index_password"], "")
        self.assertEqual(payload["gdrive_selected_folders"], [])

    def test_password_not_in_repr(self):
        self.assertNotIn("fake-secret", repr(gdi.GDIConfig(ROOT, "reader", "fake-secret")))


class Protocol(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        gdi._sessions.clear()
        self.calls = []
        self.handler = lambda req: reply(listing())
        async def dispatch(req):
            self.calls.append(req)
            return self.handler(req)
        self.factory = patch.object(httpx, "AsyncClient", side_effect=lambda *a, **kw: REAL_CLIENT(*a, transport=httpx.MockTransport(dispatch), **kw))
        self.resolver = patch.object(gdi, "resolve_public_address", AsyncMock(return_value="8.8.8.8"))
        self.factory.start(); self.resolver.start()
        self.addCleanup(self.factory.stop); self.addCleanup(self.resolver.stop)

    async def test_anonymous_listing_discards_signed_urls(self):
        self.handler = lambda req: reply(listing([item("Anime & Shows", True), item("Movie 2026.mp4")]))
        async with gdi.GDIClient(gdi.GDIConfig(ROOT)) as client:
            page = await client.list_page("/0:/")
        self.assertEqual(page["items"][0]["path"], "/0:/Anime%20%26%20Shows/")
        self.assertNotIn("link", page["items"][1])
        self.assertNotIn("id", page["items"][1])
        req = self.calls[0]
        self.assertEqual(req.url.host, "8.8.8.8")
        self.assertEqual(req.headers["host"], "index.example")
        self.assertEqual(req.extensions["sni_hostname"], "index.example")
        self.assertEqual(json.loads(req.content), {"page_token": None, "page_index": 0})

    def authenticated_worker(self, req):
        if req.url.path == "/login":
            self.assertIn(b"username=reader", req.content)
            return reply({"ok": True}, headers={"set-cookie": "session=FAKE-SESSION; HttpOnly; Secure; Path=/"})
        if req.headers.get("cookie") != "session=FAKE-SESSION":
            return reply(b"<html>Sign in</html>", headers={"Content-Type": "text/html"})
        return reply(listing([item("Movie.mp4")]))

    async def test_authenticated_listing_and_session_reuse(self):
        self.handler = self.authenticated_worker
        config = gdi.GDIConfig(ROOT, "reader", "fake-secret")
        for _ in range(2):
            async with gdi.GDIClient(config) as client:
                self.assertEqual(len((await client.list_page("/0:/"))["items"]), 1)
        self.assertEqual(sum(r.url.path == "/login" for r in self.calls), 1)
        self.assertNotIn("cookie", self.calls[1].headers)  # login carries no stale session

    async def test_login_required_clear_message(self):
        self.handler = lambda req: reply(b"<html>login</html>")
        async with gdi.GDIClient(gdi.GDIConfig(ROOT)) as client:
            with self.assertRaisesRegex(gdi.GDIError, "requires login"):
                await client.list_page("/0:/")

    async def test_failed_login_does_not_echo_remote_content(self):
        self.handler = lambda req: reply({"ok": False, "message": "secret-remote-content"}) if req.url.path == "/login" else reply({}, 401)
        async with gdi.GDIClient(gdi.GDIConfig(ROOT, "reader", "fake-secret")) as client:
            with self.assertRaises(gdi.GDIError) as error:
                await client.list_page("/0:/")
        self.assertNotIn("secret", str(error.exception))

    async def test_redirects_never_forward_credentials(self):
        self.handler = lambda req: reply({}, 307, {"location": "https://other.example/login"})
        async with gdi.GDIClient(gdi.GDIConfig(ROOT, "reader", "fake-secret")) as client:
            with self.assertRaisesRegex(gdi.GDIError, "redirected"):
                await client._login("")
        self.assertEqual(len(self.calls), 1)

    async def test_private_dns_rejected_before_http(self):
        with patch.object(gdi, "resolve_public_address", AsyncMock(side_effect=ValueError("Source must resolve only to public IP addresses"))):
            async with gdi.GDIClient(gdi.GDIConfig(ROOT)) as client:
                with self.assertRaises(ValueError):
                    await client.list_page("/0:/")
        self.assertEqual(self.calls, [])

    async def test_cross_origin_scoped_request_blocked(self):
        async with REAL_CLIENT(trust_env=False) as client:
            with self.assertRaises(gdi.GDIError):
                await gdi.open_scoped(client, "POST", "https://other.example/login", "https://index.example", content=b"fake-secret")
        self.assertEqual(self.calls, [])

    async def test_oversized_page_closed(self):
        body = reply(b"x" * 101)
        self.handler = lambda req: body
        with patch.object(gdi, "MAX_PAGE_BYTES", 100):
            async with gdi.GDIClient(gdi.GDIConfig(ROOT)) as client:
                with self.assertRaisesRegex(gdi.GDIError, "safety limit"):
                    await client.list_page("/0:/")
        self.assertTrue(body.extensions["test_body"].closed)

    async def test_child_names_cannot_escape(self):
        self.handler = lambda req: reply(listing([item("../private", True)]))
        async with gdi.GDIClient(gdi.GDIConfig(ROOT)) as client:
            with self.assertRaises(gdi.GDIError):
                await client.list_page("/0:/")

    async def test_multi_folder_pagination_recursion_filters(self):
        def worker(req):
            payload = json.loads(req.content)
            if req.url.path == "/0:/Movies/":
                if payload["page_token"]:
                    self.assertEqual(payload["page_index"], 1)
                    return reply(listing([item("Keep B.mkv")]))
                return reply(listing([item("Keep A.mp4"), item("skip.mp4"), item("Sub", True)], "page-2"))
            if req.url.path == "/0:/Movies/Sub/":
                return reply(listing([item("Keep C.mp4")]))
            return reply(listing([item("Keep Show S01E01.mkv")]))
        self.handler = worker
        progress = []
        files = await gdi.discover(gdi.GDIConfig(ROOT), ["/0:/Movies/", "/0:/Shows/", "/0:/Movies/Sub/"],
                                   includes=["Keep"], progress=lambda *args: progress.append(args))
        self.assertEqual(len(files), 4)
        self.assertTrue(all(f["kind"] == "gdi_js" and "download.aspx" not in f["url"] for f in files))
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(progress[-1][1], 4)

    async def test_repeated_token_fails_not_false_completion(self):
        self.handler = lambda req: reply(listing([], "same-token"))
        with self.assertRaisesRegex(gdi.GDIError, "repeated"):
            await gdi.discover(gdi.GDIConfig(ROOT), ["/0:/"])

    async def test_no_selection_cannot_scan_whole_drive(self):
        with self.assertRaisesRegex(gdi.GDIError, "Choose"):
            await gdi.discover(gdi.GDIConfig(ROOT), [])
        self.assertEqual(self.calls, [])

    async def test_cancelled_discovery_returns_no_partial_files(self):
        self.handler = lambda req: reply(listing([item("Movie.mp4")], "next"))
        found = await gdi.discover(gdi.GDIConfig(ROOT), ["/0:/"], cancelled=lambda: bool(self.calls))
        self.assertEqual(found, [])
        self.assertEqual(len(self.calls), 1)

    async def test_fresh_link_at_playback_preserves_range(self):
        media = reply(b"abcdef", 206, {"Content-Type": "video/mp4", "Content-Length": "6", "Content-Range": "bytes 0-5/6"})
        def worker(req):
            if req.url.path == "/download.aspx":
                self.assertEqual(req.headers.get("range"), "bytes=0-5")
                self.assertIn(b"file=FRESH", req.url.query)
                return media
            return reply({"link": "/download.aspx?file=FRESH&expiry=NOW&mac=SAFE"})
        self.handler = worker
        settings = Settings({"gdrive_index_url": ROOT})
        request = Request({"type": "http", "method": "GET", "path": "/dl/test", "headers": [(b"range", b"bytes=0-5")]})
        response = await remote_stream.remote_media_response(request, ROOT + "Movie.mp4", "Movie.mp4", opener=gdi.media_opener(settings))
        self.assertEqual(media.extensions["test_body"].reads, 0)
        self.assertEqual(response.status_code, 206)
        try:
            self.assertEqual(b"".join([c async for c in response.body_iterator]), b"abcdef")
        finally:
            await response.close()
        self.assertTrue(media.extensions["test_body"].closed)

    async def test_external_download_link_blocked(self):
        self.handler = lambda req: reply({"link": "https://other.example/download.aspx?file=x&expiry=y&mac=z"})
        async with gdi.GDIClient(gdi.GDIConfig(ROOT)) as client:
            with self.assertRaisesRegex(gdi.GDIError, "expected download"):
                await client.resolve_file(ROOT + "Movie.mp4")


class AppIntegration(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.settings = Settings({"gdrive_source_type": "gdi_js", "gdrive_index_url": ROOT,
                                  "gdrive_index_password": "fake-secret", "gdrive_selected_folders": ["/0:/Movies/"]})
        self.patch = patch.object(SettingsManager, "_current", self.settings)
        self.patch.start(); self.addCleanup(self.patch.stop)

    async def test_settings_response_masks_password(self):
        from Backend.fastapi.routes import api_routes
        with patch.object(api_routes.db, "get_database_list", return_value=[]):
            value = await api_routes.get_settings_api()
        self.assertEqual(value["settings"]["gdrive_index_password"], "")
        self.assertTrue(value["settings"]["gdrive_index_password_set"])

    async def test_scan_uses_selected_folders_and_stable_media_id(self):
        from Backend.helper import scan_manager
        manager = GDriveScanManager()
        database = AsyncMock()
        database.dbs = {}
        manager.bind_db(database)
        files = [{"name": "Movie.mp4", "url": ROOT + "Movies/Movie.mp4", "kind": "gdi_js"}]
        async def pages(*args, **kwargs):
            yield files, {"queue": [], "pages": 1, "files": 1}, "/0:/Movies/"
        with patch.object(gdi, "discover_pages", Mock(side_effect=pages)) as discover, \
             patch.object(scan_manager, "encode_string", AsyncMock(return_value="id")) as encode, \
             patch.object(manager, "_stream_id_exists", AsyncMock(return_value=False)), \
             patch.object(scan_manager, "metadata", AsyncMock(return_value={"title": "Movie"})):
            await manager._run()
        self.assertEqual(discover.call_args.args[1], ["/0:/Movies/"])
        self.assertEqual(encode.call_args.args[0]["kind"], "gdi_js")
        self.assertEqual(manager.state["status"], "completed")
        database.insert_media.assert_awaited_once()

    async def test_failure_does_not_mutate_library(self):
        manager = GDriveScanManager()
        database = AsyncMock()
        database.dbs = {}
        manager.bind_db(database)
        async def pages(*args, **kwargs):
            raise gdi.GDIError("Index requires login")
            yield
        with patch.object(gdi, "discover_pages", pages):
            await manager._run()
        self.assertEqual(manager.state["status"], "error")
        self.assertEqual(database.mock_calls, [])

    async def test_invalid_selections_are_http_400(self):
        from Backend.fastapi.routes import api_routes
        with self.assertRaises(HTTPException) as error:
            await api_routes.gdrive_folder_selection_api({"folders": ["/1:/"]})
        self.assertEqual(error.exception.status_code, 400)

    async def test_failed_save_keeps_settings_snapshot(self):
        database = AsyncMock()
        database.save_settings.return_value = False
        with patch.object(SettingsManager, "_sync_channel_titles", AsyncMock()), self.assertRaises(ValueError):
            await SettingsManager.update(database, {"gdrive_selected_folders": ["/0:/Shows/"]})
        self.assertEqual(SettingsManager.current().gdrive_selected_folders, ["/0:/Movies/"])

    async def test_stream_route_uses_gdi_opener(self):
        from Backend.fastapi.routes import stream_routes
        from starlette.responses import Response
        with patch.object(stream_routes, "remote_media_response", AsyncMock(return_value=Response())) as response, \
             patch.object(stream_routes.db, "is_indexed_gdrive_stream", AsyncMock(return_value=True)):
            await stream_routes.gdrive_media_streamer(None, ROOT + 'Movies/Test.mp4', 'Test.mp4', 'fake-token', source_kind='gdi_js')
        self.assertTrue(callable(response.call_args.kwargs['opener']))

    async def test_forged_gdi_id_cannot_use_server_credentials(self):
        from Backend.fastapi.routes import stream_routes
        with patch.object(stream_routes.db, "is_indexed_gdrive_stream", AsyncMock(return_value=False)), \
             patch.object(stream_routes, "remote_media_response", AsyncMock()) as response:
            with self.assertRaises(HTTPException) as error:
                await stream_routes.gdrive_media_streamer(None, ROOT + 'Private.txt', 'Private.txt', 'fake-token', source_kind='gdi_js')
        self.assertEqual(error.exception.status_code, 404)
        response.assert_not_awaited()

    async def test_indexed_stream_query_matches_one_quality(self):
        from Backend.helper.database import Database
        database = Database()
        collection = AsyncMock()
        collection.find_one.return_value = {"_id": "existing"}
        database.dbs = {"storage_1": {"movie": collection}}
        self.assertTrue(await database.is_indexed_gdrive_stream("saved-id", ROOT + "Movie.mp4"))
        collection.find_one.assert_awaited_once_with(
            {"telegram": {"$elemMatch": {"id": "saved-id", "source": "gdrive", "source_url": ROOT + "Movie.mp4"}}},
            {"_id": 1},
        )
        self.assertFalse(await database.is_indexed_gdrive_stream("", ROOT + "Movie.mp4"))

    def test_backup_excludes_index_password(self):
        from Backend.helper.backup import _SETTINGS_EXCLUDE
        self.assertIn("gdrive_index_password", _SETTINGS_EXCLUDE)

    def test_folder_endpoints_require_admin_auth(self):
        import ast
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'Backend/fastapi/main.py').read_text())
        for name in ('tools_gdrive_folder_config', 'tools_gdrive_folders', 'tools_gdrive_folder_selection'):
            function = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
            self.assertTrue(any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == 'Depends'
                                and any(isinstance(a, ast.Name) and a.id == 'require_auth' for a in n.args)
                                for n in ast.walk(function)))
