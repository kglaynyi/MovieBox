"""Offline regressions. Run with .venv/bin/python -m unittest discover -s tests -v."""
import asyncio
import ast
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

# Importing Backend constructs clients, but these tests never start a real client.
os.environ.update(API_ID="12345", API_HASH="0" * 32, BOT_TOKEN="12345:dummy",
                  OWNER_ID="12345", DATABASE="mongodb://localhost:27017,mongodb://localhost:27017")

import httpx
from fastapi import HTTPException
from starlette.requests import Request
from starlette.requests import ClientDisconnect
from starlette.middleware.sessions import SessionMiddleware
from jinja2 import Environment, FileSystemLoader

from Backend.helper import remote_http, remote_stream, gdrive_source
from Backend.helper.database import Database
from Backend.helper.settings_manager import Settings, SettingsManager
from Backend.helper.scan_manager import GDriveScanManager
from Backend.pyrofork import clients

ROOT = Path(__file__).resolve().parents[1]
REAL_CLIENT = httpx.AsyncClient


class Body(httpx.AsyncByteStream):
    def __init__(self, data=b"abcdef"):
        self.data = data
        self.reads = 0
        self.closed = False

    async def __aiter__(self):
        self.reads += 1
        yield self.data

    async def aclose(self):
        self.closed = True


def response(status=200, data=b"abcdef", headers=None):
    body = Body(data)
    return httpx.Response(status, headers=headers or {}, stream=body, extensions={"test_body": body})


def request(method="GET", headers=None):
    return Request({"type": "http", "method": method, "path": "/video",
                    "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]})


class RemoteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.resolver = patch.object(remote_http, "resolve_public_address", AsyncMock(return_value="8.8.8.8"))
        self.resolver.start()
        self.addCleanup(self.resolver.stop)

    async def build(self, handler, req=None):
        def client_factory(*args, **kwargs):
            return REAL_CLIENT(*args, transport=httpx.MockTransport(handler), **kwargs)
        with patch.object(remote_stream.httpx, "AsyncClient", side_effect=client_factory):
            return await remote_stream.remote_media_response(req or request(), "https://index.example/video.mkv", "Movie.mkv")

    async def test_full_stream_is_one_unbuffered_request(self):
        calls = []
        upstream = response(headers={"Content-Length": "6", "Content-Type": "video/mp4"})
        def handler(req):
            calls.append(req)
            return upstream
        out = await self.build(handler)
        self.assertEqual(upstream.extensions["test_body"].reads, 0)
        self.assertEqual(out.status_code, 200)
        self.assertEqual(out.headers["content-length"], "6")
        self.assertEqual(out.headers["cache-control"], "private, no-store")
        try:
            self.assertEqual(b"".join([c async for c in out.body_iterator]), b"abcdef")
        finally:
            await out.close()
        self.assertEqual(len(calls), 1)
        self.assertNotIn("range", calls[0].headers)
        self.assertEqual(calls[0].url.host, "8.8.8.8")
        self.assertEqual(calls[0].headers["host"], "index.example")
        self.assertEqual(calls[0].extensions["sni_hostname"], "index.example")
        self.assertTrue(upstream.extensions["test_body"].closed)
        self.assertTrue(out.client.is_closed)

    async def test_range_status_and_bytes_are_preserved(self):
        seen = []
        def handler(req):
            seen.append(req)
            return response(206, b"bcd", {"Content-Length": "3", "Content-Range": "bytes 1-3/6"})
        out = await self.build(handler, request(headers={"Range": "bytes=1-3", "If-Range": '"tag"'}))
        try:
            self.assertEqual(out.status_code, 206)
            self.assertEqual(out.headers["content-range"], "bytes 1-3/6")
            self.assertEqual(b"".join([c async for c in out.body_iterator]), b"bcd")
        finally:
            await out.close()
        self.assertEqual(seen[0].headers["range"], "bytes=1-3")
        self.assertEqual(seen[0].headers["if-range"], '"tag"')

    async def test_ignored_range_stays_200_and_unbuffered(self):
        upstream = response(headers={"Content-Length": "6"})
        out = await self.build(lambda req: upstream, request(headers={"Range": "bytes=0-0"}))
        self.assertEqual(out.status_code, 200)
        self.assertEqual(out.headers["content-length"], "6")
        self.assertEqual(upstream.extensions["test_body"].reads, 0)
        await out.close()

    async def test_head_fallback_does_not_download_or_report_one_byte(self):
        calls, replies = [], []
        def handler(req):
            calls.append(req.method)
            reply = response(405 if req.method == "HEAD" else 200, headers={"Content-Length": "6000000000"})
            replies.append(reply)
            return reply
        out = await self.build(handler, request("HEAD"))
        self.assertEqual(calls, ["HEAD", "GET"])
        self.assertEqual(out.headers["content-length"], "6000000000")
        self.assertEqual(out.body, b"")
        self.assertTrue(all(r.extensions["test_body"].closed and r.extensions["test_body"].reads == 0 for r in replies))

    async def test_unknown_head_length_is_not_zero(self):
        out = await self.build(lambda req: response(), request("HEAD"))
        self.assertNotIn("content-length", out.headers)

    async def test_unsatisfiable_range(self):
        upstream = response(416, headers={"Content-Range": "bytes */6"})
        out = await self.build(lambda req: upstream, request(headers={"Range": "bytes=99-"}))
        self.assertEqual(out.status_code, 416)
        self.assertEqual(out.headers["content-range"], "bytes */6")
        self.assertTrue(upstream.extensions["test_body"].closed)

    async def test_errors_happen_before_streaming(self):
        for status, mime, expected in [(403, "", 502), (404, "", 404), (200, "text/html", 502)]:
            upstream = response(status, headers={"Content-Type": mime})
            with self.assertRaises(HTTPException) as error:
                await self.build(lambda req: upstream)
            self.assertEqual(error.exception.status_code, expected)
            self.assertTrue(upstream.extensions["test_body"].closed)
            self.assertEqual(upstream.extensions["test_body"].reads, 0)

    async def test_disconnect_closes_response_and_client(self):
        upstream = response()
        out = await self.build(lambda req: upstream)
        async def send(message):
            raise OSError("Client disconnected")
        with self.assertRaises(ClientDisconnect):
            await out({"type": "http", "asgi": {"spec_version": "2.4"}}, AsyncMock(), send)
        self.assertTrue(out.client.is_closed)
        self.assertTrue(upstream.extensions["test_body"].closed)

    async def test_redirect_to_private_address_is_blocked(self):
        calls = []
        upstream = response(302, headers={"Location": "http://127.0.0.1/admin"})
        def handler(req):
            calls.append(req)
            return upstream
        with self.assertRaises(HTTPException):
            await self.build(handler)
        self.assertEqual(len(calls), 1)
        self.assertTrue(upstream.extensions["test_body"].closed)

    async def test_relative_redirect_is_validated_and_pinned(self):
        calls = []
        def handler(req):
            calls.append(req)
            return response(302, headers={"Location": "/download.mkv"}) if len(calls) == 1 else response()
        out = await self.build(handler)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1].url.path, "/download.mkv")
        await out.close()

    async def test_index_size_is_bounded(self):
        upstream = response(data=b"123456")
        async with REAL_CLIENT(transport=httpx.MockTransport(lambda req: upstream), trust_env=False) as client:
            with self.assertRaises(ValueError):
                await remote_http.read_text(client, "https://index.example/", limit=5)
        self.assertTrue(upstream.extensions["test_body"].closed)

    async def test_link_check_never_consumes_the_video(self):
        upstream = response(headers={"Content-Length": "9000000000"})
        with patch.object(gdrive_source, "open_remote", AsyncMock(return_value=upstream)):
            self.assertTrue(await gdrive_source.check_remote_stream_alive("https://index.example/movie.mkv"))
        self.assertEqual(upstream.extensions["test_body"].reads, 0)
        self.assertTrue(upstream.extensions["test_body"].closed)

    async def test_transient_failure_is_not_dead(self):
        for status in (401, 403, 429, 500, 503):
            upstream = response(status)
            with patch.object(gdrive_source, "open_remote", AsyncMock(return_value=upstream)):
                self.assertIsNone(await gdrive_source.check_remote_stream_alive("https://index.example/movie.mkv"))

    async def test_include_filters_do_not_prevent_directory_traversal(self):
        pages = {"https://index.example/0:/": '<a href="series/">Series</a>',
                 "https://index.example/0:/series/": '<a href="../">Parent</a><a href="Naruto.mp4">Naruto.mp4</a>'}
        with patch.object(gdrive_source, "_fetch_text", AsyncMock(side_effect=lambda client, url: pages[url])):
            found = await gdrive_source._crawl_index(None, "https://index.example/0:/", ["naruto"], [])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["name"], "Naruto.mp4")


class URLTests(unittest.IsolatedAsyncioTestCase):
    def test_private_and_credential_urls_rejected(self):
        for url in ("file:///etc/passwd", "http://localhost/", "http://x.localhost/", "http://127.0.0.1/",
                    "http://169.254.169.254/", "http://100.64.0.1/", "http://[::1]/", "http://224.0.0.1/",
                    "https://user:password@example.com/"):
            self.assertFalse(remote_http.public_url(url), url)

    async def test_dns_private_or_mixed_answers_rejected(self):
        for addresses in (("127.0.0.1",), ("8.8.8.8", "10.0.0.1")):
            records = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in addresses]
            with patch.object(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(return_value=records)):
                with self.assertRaises(remote_http.UnsafeRemoteURL):
                    await remote_http.resolve_public_address("example.com", 443)

    def test_only_drive_hosts_are_rewritten(self):
        url = "https://index.example/video.mp4?id=abc"
        self.assertEqual(gdrive_source.normalize_drive_download_url(url), url)
        self.assertIsNone(gdrive_source.extract_drive_folder_id(url))
        self.assertEqual(gdrive_source.normalize_drive_download_url("https://drive.google.com/file/d/abc/view"),
                         "https://drive.google.com/uc?export=download&id=abc")


class ScanTests(unittest.IsolatedAsyncioTestCase):
    async def test_quality_refresh_preserves_other_sources(self):
        db = Database()
        telegram = {"id": "tg", "source": "telegram", "quality": "1080p"}
        old = {"id": "old", "source": "gdrive", "source_url": "https://example.com/a", "quality": "1080p"}
        other = {"id": "other", "source": "gdrive", "source_url": "https://example.com/b", "quality": "1080p"}
        new = dict(old, id="new")
        for replace in (True, False):
            with patch.object(SettingsManager, "current", return_value=Settings({"replace_mode": replace})):
                result = await db._apply_quality_update([telegram, old, other], new)
                result = await db._apply_quality_update(result, new)
            self.assertEqual(result, [telegram, other, new])

    async def test_rescan_failure_does_not_delete_existing_media(self):
        manager = GDriveScanManager()
        db = SimpleNamespace(insert_media=AsyncMock(), dbs={}, current_db_index=1)
        manager.bind_db(db)
        manager.state.update(mode="rescan", status="running")
        with patch("Backend.helper.scan_manager.discover_gdrive_files", AsyncMock(return_value=[
                {"name": "Movie.mp4", "url": "https://example.com/movie.mp4"}])), \
             patch("Backend.helper.scan_manager.metadata", AsyncMock(return_value=None)):
            await manager._run()
        db.insert_media.assert_not_called()
        self.assertEqual(manager.state["counters"]["skipped_meta"], 1)
        self.assertFalse(hasattr(manager, "_purge_existing_gdrive_entries"))

    async def test_drive_removal_does_not_delete_telegram_messages(self):
        with patch("Backend.helper.database.decode_string", AsyncMock()) as decode:
            await Database()._queue_quality_deletion({"id": "drive", "source": "gdrive"})
            decode.assert_not_called()


class StartupTests(unittest.IsolatedAsyncioTestCase):
    def test_all_python_sources_compile(self):
        sources = list((ROOT / "Backend").rglob("*.py")) + list(ROOT.glob("*.py"))
        for source in sources:
            with self.subTest(path=source.relative_to(ROOT)):
                compile(source.read_text(), str(source), "exec")

    def test_all_templates_parse_and_route_templates_exist(self):
        directory = ROOT / "Backend/fastapi/templates"
        env = Environment(loader=FileSystemLoader(directory))
        for source in directory.glob("*.html"):
            with self.subTest(path=source.name):
                env.parse(source.read_text())
        for source in (ROOT / "Backend/fastapi/routes").glob("*.py"):
            for node in ast.walk(ast.parse(source.read_text())):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "TemplateResponse" and node.args
                        and isinstance(node.args[0], ast.Constant)):
                    self.assertTrue((directory / node.args[0].value).exists(), node.args[0].value)

    async def test_expired_and_over_quota_tokens_cannot_download_directly(self):
        from Backend.fastapi.security import tokens
        for data in ({"subscription_expired": True}, {"limit_exceeded": "daily"}):
            with patch.object(tokens, "verify_token", AsyncMock(return_value=data)):
                with self.assertRaises(HTTPException) as error:
                    await tokens.require_stream_token("dummy")
                self.assertEqual(error.exception.status_code, 403)
        with patch.object(tokens, "verify_token", AsyncMock(return_value={"token": "valid"})):
            self.assertEqual(await tokens.require_stream_token("valid"), {"token": "valid"})

    async def test_public_status_and_guide_render(self):
        from Backend.fastapi.routes import template_routes
        req = request()
        req.scope["session"] = {}
        with patch.object(template_routes.db, "get_database_stats", AsyncMock(return_value={})), \
             patch.object(template_routes.db, "content_totals", return_value=(0, 0)):
            status = await template_routes.public_status_page(req)
        guide = await template_routes.stremio_guide_page(req)
        self.assertEqual(status.status_code, 200)
        self.assertIn(b"Server Status", status.body)
        self.assertIn(b"Watch with Stremio", guide.body)

    async def test_failed_extra_client_does_not_crash_startup(self):
        with patch.object(clients.TokenParser, "parse_from_settings", return_value={1: "bad", 2: "good"}), \
             patch.object(clients, "start_client", AsyncMock(side_effect=[None, (2, "good-client")])), \
             patch.object(clients.StreamBot.storage, "dc_id", AsyncMock(return_value=1)), \
             patch.dict(clients.multi_clients, {}, clear=True), patch.dict(clients.client_tokens, {}, clear=True):
            await clients.initialize_clients()
            self.assertNotIn(1, clients.multi_clients)
            self.assertEqual(clients.multi_clients[2], "good-client")

    async def test_failed_extra_client_does_not_crash_reload(self):
        with patch.object(clients.TokenParser, "parse_from_settings", return_value={1: "bad"}), \
             patch.object(clients, "start_client", AsyncMock(return_value=None)), \
             patch.dict(clients.client_tokens, {}, clear=True):
            result = await clients.reload_multi_token_clients()
            self.assertNotIn(1, clients.client_tokens)
            self.assertEqual(result["started"], 0)

    def test_legacy_upstream_settings_are_ignored(self):
        settings = Settings({"upstream_repo": "https://example.com", "upstream_branch": "main", "base_url": "https://movie.example"})
        self.assertNotIn("upstream_repo", settings.to_dict())
        self.assertNotIn("upstream_branch", settings.to_dict())
        self.assertEqual(settings.base_url, "https://movie.example")

    def test_compatibility_updater_is_side_effect_free(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git").mkdir()
            (root / ".git" / "sentinel").write_text("keep")
            (root / "log.txt").write_text("keep logs")
            result = subprocess.run([sys.executable, str(ROOT / "update.py")], cwd=root, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0)
            self.assertEqual((root / ".git" / "sentinel").read_text(), "keep")
            self.assertEqual((root / "log.txt").read_text(), "keep logs")

    async def test_restart_uses_current_interpreter_without_updater(self):
        from Backend.fastapi.routes import api_routes
        with patch.object(api_routes.asyncio, "sleep", AsyncMock()), patch.object(api_routes.os, "execl") as execute:
            await api_routes._perform_restart()
        execute.assert_called_once_with(sys.executable, sys.executable, "-m", "Backend")

    async def test_requests_gated_during_boot_and_stats_require_login(self):
        from Backend.fastapi.main import app
        if not any(m.cls is SessionMiddleware for m in app.user_middleware):
            app.add_middleware(SessionMiddleware, secret_key="test-only")
        old_ready = app.state.services_ready
        try:
            async with REAL_CLIENT(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                app.state.services_ready = False
                result = await client.get("/stream/stats")
                self.assertEqual(result.status_code, 503)
                self.assertEqual(result.headers["retry-after"], "5")
                app.state.services_ready = True
                for path in ("/stream/stats", "/stream/stats/secret"):
                    result = await client.get(path)
                    self.assertIn(result.status_code, (401, 302, 303, 307))
        finally:
            app.state.services_ready = old_ready


if __name__ == "__main__":
    unittest.main()
