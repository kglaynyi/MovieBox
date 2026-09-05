"""Per-token registered-device and playback-slot limits."""
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase

from Backend.helper.device_control import acquire_playback_slot, register_device


class Collection:
    def __init__(self, doc):
        self.doc = doc

    async def find_one(self, query):
        return deepcopy(self.doc) if query.get("token") == self.doc["token"] else None

    async def update_one(self, query, update):
        if query.get("token") != self.doc["token"]:
            return SimpleNamespace(matched_count=0, modified_count=0)
        self.doc.update(deepcopy(update.get("$set") or {}))
        return SimpleNamespace(matched_count=1, modified_count=1)


def database(doc):
    return SimpleNamespace(dbs={"tracking": {"api_tokens": Collection(doc)}})


def request(ip, agent="Nuvio/1 Android"):
    return SimpleNamespace(
        headers={"cf-connecting-ip": ip, "user-agent": agent},
        client=SimpleNamespace(host=ip),
    )


class DeviceControlTests(IsolatedAsyncioTestCase):
    async def test_registered_device_limit_and_repeat_access(self):
        db = database({"token": "t", "limits": {"max_devices": 1}, "devices": []})
        allowed, first = await register_device(db, "t", request("1.1.1.1"))
        self.assertTrue(allowed)
        self.assertTrue((await register_device(db, "t", request("1.1.1.1")))[0])
        allowed, second = await register_device(db, "t", request("2.2.2.2"))
        self.assertFalse(allowed)
        self.assertNotEqual(first, second)

    async def test_concurrent_limit_counts_devices_not_range_requests(self):
        db = database({"token": "t", "limits": {"max_concurrent_streams": 1, "max_devices": 0}})
        self.assertTrue(await acquire_playback_slot(db, "t", "phone"))
        self.assertTrue(await acquire_playback_slot(db, "t", "phone"))
        self.assertFalse(await acquire_playback_slot(db, "t", "tv"))

    async def test_expired_playback_lease_releases_slot(self):
        db = database({
            "token": "t", "limits": {"max_concurrent_streams": 1},
            "playback_sessions": [{"device_id": "phone", "last_seen": datetime.utcnow() - timedelta(hours=1)}],
        })
        self.assertTrue(await acquire_playback_slot(db, "t", "tv"))
        self.assertEqual(db.dbs["tracking"]["api_tokens"].doc["playback_sessions"][0]["device_id"], "tv")

    async def test_unlimited_concurrency_does_not_create_lease(self):
        db = database({"token": "t", "limits": {"max_concurrent_streams": 0}})
        self.assertTrue(await acquire_playback_slot(db, "t", "phone"))
        self.assertNotIn("playback_sessions", db.dbs["tracking"]["api_tokens"].doc)

    async def test_device_reset_revokes_old_playback_url(self):
        db = database({
            "token": "t", "limits": {"max_devices": 1, "max_concurrent_streams": 1},
            "devices": [],
        })
        self.assertFalse(await acquire_playback_slot(db, "t", "old-device"))
