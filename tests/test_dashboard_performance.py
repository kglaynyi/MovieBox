"""Dashboard must not wait for remote databases to render its shell."""
import asyncio
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace
from starlette.requests import Request

from test_gdi_js import ROOT  # initializes offline test configuration
from Backend.helper.database import Database
from Backend.fastapi.routes import template_routes


class DashboardPerformanceTests(IsolatedAsyncioTestCase):
    async def test_shell_does_not_query_database(self):
        request = Request({'type': 'http', 'method': 'GET', 'path': '/',
                           'headers': [], 'session': {'authenticated': True}})
        with patch.object(template_routes.db, 'get_database_stats', new_callable=AsyncMock) as query, patch.object(template_routes, 'StreamBot', SimpleNamespace(username='testbot')):
            response = await template_routes.dashboard_page(request, True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['system_stats']['server_status'], 'running')
        query.assert_not_awaited()
        self.assertIn(b'Loading storage statistics', response.body)

    async def test_concurrent_requests_share_cache(self):
        db = Database()
        expected = [{'db_name': 'storage_1'}]
        with patch.object(db, '_fetch_database_stats', AsyncMock(return_value=expected)) as query:
            results = await asyncio.gather(*(db.get_database_stats() for _ in range(8)))
            self.assertEqual(results, [expected] * 8)
            query.assert_awaited_once()
            db._stats_cached_at -= 31
            await db.get_database_stats()
            self.assertEqual(query.await_count, 2)

    async def test_failure_is_not_cached(self):
        db = Database()
        with patch.object(db, '_fetch_database_stats', AsyncMock(side_effect=[RuntimeError('offline'), []])) as query:
            with self.assertRaises(RuntimeError):
                await db.get_database_stats()
            self.assertEqual(await db.get_database_stats(), [])
            self.assertEqual(query.await_count, 2)
