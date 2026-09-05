"""Per-token device registration and approximate concurrent-playback control."""
from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta

from Backend.helper.analytics import client_ip_from, parse_app, parse_device

_LOCK = asyncio.Lock()
PLAYBACK_LEASE_MINUTES = 30


def positive_int(value) -> int:
    try:
        value = int(value or 0)
        return value if value > 0 else 0
    except (TypeError, ValueError):
        return 0


def device_signature(token: str, request) -> tuple[str, dict]:
    ip = client_ip_from(request)
    agent = request.headers.get("user-agent", "")
    digest = hashlib.sha256(f"{token}\0{ip}\0{agent}".encode()).hexdigest()[:24]
    return digest, {
        "id": digest,
        "app": parse_app(agent),
        "device": parse_device(agent) or "Unknown device",
        "user_agent": agent[:240],
        "first_seen": datetime.utcnow(),
        "last_seen": datetime.utcnow(),
    }


async def register_device(database, token: str, request) -> tuple[bool, str]:
    """Register/touch this client. Returns (allowed, stable device id)."""
    device_id, entry = device_signature(token, request)
    coll = database.dbs["tracking"]["api_tokens"]
    async with _LOCK:
        doc = await coll.find_one({"token": token}) or {}
        devices = list(doc.get("devices") or [])
        existing = next((d for d in devices if d.get("id") == device_id), None)
        if existing:
            existing.update({"last_seen": entry["last_seen"], "app": entry["app"],
                             "device": entry["device"], "user_agent": entry["user_agent"]})
        else:
            maximum = positive_int((doc.get("limits") or {}).get("max_devices"))
            if maximum and len(devices) >= maximum:
                return False, device_id
            devices.append(entry)
        await coll.update_one({"token": token}, {"$set": {"devices": devices[-50:]}})
    return True, device_id


async def acquire_playback_slot(database, token: str, device_id: str) -> bool:
    """Acquire/refresh one device lease; repeated HTTP ranges share the lease."""
    if not device_id:
        return False
    coll = database.dbs["tracking"]["api_tokens"]
    now = datetime.utcnow()
    cutoff = now - timedelta(minutes=PLAYBACK_LEASE_MINUTES)
    async with _LOCK:
        doc = await coll.find_one({"token": token}) or {}
        limits = doc.get("limits") or {}
        maximum = positive_int(limits.get("max_concurrent_streams"))
        if not maximum:
            return True
        # A reset revokes previously issued playback URLs until the client asks
        # the addon for streams again and registers itself.
        if positive_int(limits.get("max_devices")) and not any(
            d.get("id") == device_id for d in (doc.get("devices") or [])
        ):
            return False
        sessions = [s for s in (doc.get("playback_sessions") or [])
                    if s.get("last_seen") and s["last_seen"] >= cutoff]
        existing = next((s for s in sessions if s.get("device_id") == device_id), None)
        if existing:
            existing["last_seen"] = now
        elif maximum and len(sessions) >= maximum:
            await coll.update_one({"token": token}, {"$set": {"playback_sessions": sessions}})
            return False
        else:
            sessions.append({"device_id": device_id, "last_seen": now})
        await coll.update_one({"token": token}, {"$set": {"playback_sessions": sessions}})
    return True


async def reset_token_devices(database, token: str) -> bool:
    result = await database.dbs["tracking"]["api_tokens"].update_one(
        {"token": token}, {"$set": {"devices": [], "playback_sessions": []}}
    )
    return bool(result.matched_count)
