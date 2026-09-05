"""Validation and status regressions for token-management actions."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from test_gdi_js import ROOT  # initializes offline configuration
from Backend.fastapi.routes import api_routes
from Backend.helper.custom_dl import ACTIVE_STREAMS
from Backend.helper.utils import _stop_stream_at_limit


class AccessValidationTests(TestCase):
    def test_extend_and_reduce_require_positive_days(self):
        for action, value in (("extend", 0), ("reduce", -1), ("extend", "bad"), ("set", 1.5)):
            with self.subTest(action=action, value=value), self.assertRaises(HTTPException) as raised:
                api_routes._parse_days({"days": value}, action)
            self.assertEqual(raised.exception.status_code, 400)

    def test_set_allows_zero_for_never_expires(self):
        self.assertEqual(api_routes._parse_days({"days": 0}, "set"), 0)

    def test_active_stream_is_marked_for_termination(self):
        ACTIVE_STREAMS["limit-test"] = {"status": "active"}
        ACTIVE_STREAMS["limit-test-p0"] = {"status": "active"}
        try:
            _stop_stream_at_limit("limit-test")
            self.assertTrue(ACTIVE_STREAMS["limit-test"]["limit_reached"])
            self.assertTrue(ACTIVE_STREAMS["limit-test-p0"]["limit_reached"])
        finally:
            ACTIVE_STREAMS.pop("limit-test", None)
            ACTIVE_STREAMS.pop("limit-test-p0", None)


class AccessApiTests(IsolatedAsyncioTestCase):
    async def test_missing_token_limit_update_is_404(self):
        with patch.object(api_routes.db, "update_api_token_limits", AsyncMock(return_value=False)):
            with self.assertRaises(HTTPException) as raised:
                await api_routes.update_token_limits_api("missing", {})
        self.assertEqual(raised.exception.status_code, 404)

    async def test_expired_token_displays_expired_when_subscription_off(self):
        token = {
            "token": "t", "name": "Test", "expires_at": datetime.utcnow() - timedelta(days=1),
            "created_at": datetime.utcnow(), "limits": {}, "usage": {},
        }
        fake_settings = SimpleNamespace(subscription=False, base_url="https://example.test")
        with patch.object(api_routes.db, "get_all_api_tokens", AsyncMock(return_value=[token])), \
             patch.object(api_routes.SettingsManager, "current", return_value=fake_settings):
            result = await api_routes.get_all_tokens_api()
        self.assertTrue(result["tokens"][0]["is_expired"])

    async def test_expiry_rejects_invalid_action_before_database_write(self):
        with patch.object(api_routes.db, "update_token_expiry", AsyncMock()) as update:
            with self.assertRaises(HTTPException) as raised:
                await api_routes.set_token_expiry_api("t", {"action": "erase", "days": 1})
        self.assertEqual(raised.exception.status_code, 400)
        update.assert_not_awaited()
