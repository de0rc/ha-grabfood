"""
bridge.py — Pushes GrabFood order data to Home Assistant as sensors.
"""

import logging
import os
from typing import Any, Optional

import aiohttp

logger = logging.getLogger(__name__)

HA_API_BASE = "http://supervisor/core/api"
HA_WS_URL = "ws://supervisor/core/websocket"
SENSOR_PREFIX = "sensor.grabfood"

ORDERS_ENTITY = f"{SENSOR_PREFIX}_orders"

# Must match version in config.yaml and app/www/build_card.py _VERSION constant.
ADDON_VERSION = "0.2.1"
CARD_URL = f"/local/grabfood-map-card.js?v={ADDON_VERSION}"
CARD_URL_BASE = "/local/grabfood-map-card.js"

# Entities created by older add-on versions that no longer exist.
# Deleted once on startup so they don't linger as stale/unavailable entities.
_LEGACY_ENTITIES = [
    "sensor.grabfood_order_status",
    "sensor.grabfood_eta",
    "sensor.grabfood_eta_minutes",
    "sensor.grabfood_restaurant",
    "sensor.grabfood_active_order",
    "sensor.grabfood_order_id",
    "device_tracker.grabfood_driver",
]


async def register_lovelace_resource(supervisor_token: str) -> None:
    """Register/update grabfood-map-card.js Lovelace resource with versioned URL.

    Uses a versioned query string (?v=X.Y.Z) so HA's service worker treats each
    add-on version as a new resource and always fetches it fresh from disk.
    On each startup: finds any existing grabfood-map-card.js entry (any version),
    updates it to the current versioned URL, or creates it if missing.
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(HA_WS_URL) as ws:
                msg = await ws.receive_json()
                if msg.get("type") != "auth_required":
                    raise RuntimeError(f"Unexpected first WS message: {msg}")
                await ws.send_json({"type": "auth", "access_token": supervisor_token})
                msg = await ws.receive_json()
                if msg.get("type") != "auth_ok":
                    raise RuntimeError(f"WS auth failed: {msg}")
                await ws.send_json({"id": 1, "type": "lovelace/resources"})
                msg = await ws.receive_json()
                existing = msg.get("result") or []

                # Find any existing entry for this card (any version)
                existing_entry = next(
                    (r for r in existing if CARD_URL_BASE in r.get("url", "")),
                    None,
                )
                if existing_entry and existing_entry.get("url") == CARD_URL:
                    logger.debug("Lovelace resource already at current version: %s", CARD_URL)
                    return

                if existing_entry:
                    # Update to new versioned URL
                    await ws.send_json({
                        "id": 2,
                        "type": "lovelace/resources/update",
                        "resource_id": existing_entry["id"],
                        "res_type": "module",
                        "url": CARD_URL,
                    })
                    msg = await ws.receive_json()
                    if not msg.get("success"):
                        raise RuntimeError(f"Update failed: {msg}")
                    logger.info("Updated Lovelace resource to: %s", CARD_URL)
                else:
                    # Create new entry
                    await ws.send_json({
                        "id": 2,
                        "type": "lovelace/resources/create",
                        "res_type": "module",
                        "url": CARD_URL,
                    })
                    msg = await ws.receive_json()
                    if not msg.get("success"):
                        raise RuntimeError(f"Create failed: {msg}")
                    logger.info("Registered Lovelace resource: %s", CARD_URL)
    except Exception as e:
        logger.warning("Could not register Lovelace resource via WS: %s", e)


async def cleanup_legacy_entities(session: aiohttp.ClientSession, supervisor_token: str) -> None:
    """Delete entities created by older add-on versions that no longer exist.

    Uses DELETE /api/states/<entity_id>. 404 responses are silently ignored —
    the entity simply wasn't present. Called once on startup.
    """
    headers = {
        "Authorization": f"Bearer {supervisor_token}",
        "Content-Type": "application/json",
    }
    removed = []
    for entity_id in _LEGACY_ENTITIES:
        try:
            async with session.delete(
                f"{HA_API_BASE}/states/{entity_id}",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    removed.append(entity_id)
                elif resp.status != 404:
                    text = await resp.text()
                    logger.warning(
                        "Unexpected response deleting %s: HTTP %s — %s",
                        entity_id, resp.status, text[:200],
                    )
        except Exception as e:
            logger.warning("Error deleting legacy entity %s: %s", entity_id, e)

    if removed:
        logger.info("Removed %d legacy entity/entities: %s", len(removed), ", ".join(removed))
    else:
        logger.debug("No legacy entities found to remove.")


async def push_orders_sensor(session: aiohttp.ClientSession, orders: list[dict], supervisor_token: str) -> None:
    """Push sensor.grabfood_orders — state is active order count, attributes hold full order list."""
    headers = {
        "Authorization": f"Bearer {supervisor_token}",
        "Content-Type": "application/json",
    }
    active_count = sum(1 for o in orders if o.get("active_order"))
    # Serialise orders for HA attributes — replace None with "unknown" for cleaner display
    serialised = [
        {k: (v if v is not None else "unknown") for k, v in o.items()
         if k not in ("driver_lat", "driver_lon")}
        for o in orders
    ]
    payload: dict[str, Any] = {
        "state": active_count,
        "attributes": {
            "friendly_name": "GrabFood Orders",
            "icon": "mdi:food-takeout-box",
            "orders": serialised,
            "active_count": active_count,
        },
    }
    try:
        async with session.post(
            f"{HA_API_BASE}/states/{ORDERS_ENTITY}",
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status not in (200, 201):
                text = await resp.text()
                logger.warning("Failed to push orders sensor: HTTP %s — %s", resp.status, text[:200])
            else:
                logger.info(
                    "Pushed sensor.grabfood_orders — %d order(s), %d active.",
                    len(orders), active_count
                )
    except Exception as e:
        logger.warning("Error pushing orders sensor: %s", e)


async def send_token_expired_notification(session: aiohttp.ClientSession, supervisor_token: str) -> None:
    headers = {
        "Authorization": f"Bearer {supervisor_token}",
        "Content-Type": "application/json",
    }

    payload = {
        "title": "GrabFood Tracker — Re-authentication Required",
        "message": (
            "Your GrabFood session has expired and could not be renewed automatically.\n\n"
            "[Open GrabFood Tracker](/hassio/ingress/grabfood_tracker) and log in again."
        ),
        "notification_id": "grabfood_tracker_token_expired",
    }
    try:
        async with session.post(
            f"{HA_API_BASE}/services/persistent_notification/create",
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                logger.info("Token expiry notification sent to HA.")
            else:
                text = await resp.text()
                logger.warning(
                    "Failed to send HA notification: HTTP %s — %s", resp.status, text[:200]
                )
    except Exception as e:
        logger.warning("Error sending HA notification: %s", e)


async def clear_token_expired_notification(session: aiohttp.ClientSession, supervisor_token: str) -> None:
    headers = {
        "Authorization": f"Bearer {supervisor_token}",
        "Content-Type": "application/json",
    }

    try:
        async with session.post(
            f"{HA_API_BASE}/services/persistent_notification/dismiss",
            json={"notification_id": "grabfood_tracker_token_expired"},
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                logger.debug("Token expiry notification dismissed.")
    except Exception as e:
        logger.debug("Could not dismiss notification: %s", e)


async def fire_state_change_event(session: aiohttp.ClientSession, data: dict, supervisor_token: str) -> None:
    """Fire grabfood_order_state_changed HA event when order status transitions."""
    headers = {
        "Authorization": f"Bearer {supervisor_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "order_status": data.get("order_status"),
        "order_id": data.get("order_id"),
        "restaurant": data.get("restaurant"),
        "active_order": data.get("active_order"),
    }
    try:
        async with session.post(
            f"{HA_API_BASE}/events/grabfood_order_state_changed",
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                logger.debug(
                    "Fired grabfood_order_state_changed: %s", data.get("order_status")
                )
            else:
                text = await resp.text()
                logger.warning(
                    "Failed to fire state change event: HTTP %s — %s", resp.status, text[:200]
                )
    except Exception as e:
        logger.warning("Error firing state change event: %s", e)


async def restart_addon(session: aiohttp.ClientSession, supervisor_token: str) -> None:
    """Ask the HA supervisor to restart this add-on.

    Called after a successful login or silent reauth to reclaim the memory that
    Python's allocator retains after running Playwright. The session is already
    saved to disk before this is called so nothing is lost. The supervisor brings
    the add-on back up within a few seconds.
    """
    if not supervisor_token:
        logger.debug("restart_addon skipped — SUPERVISOR_TOKEN not set.")
        return

    logger.info("Requesting supervisor restart to reclaim memory after browser session...")
    try:
        async with session.post(
            "http://supervisor/addons/self/restart",
            headers={
                "Authorization": f"Bearer {supervisor_token}",
                "Content-Type": "application/json",
            },
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                logger.info("Supervisor restart requested successfully.")
            else:
                text = await resp.text()
                logger.warning(
                    "Supervisor restart request failed: HTTP %s — %s", resp.status, text[:200]
                )
    except Exception as e:
        logger.warning("Could not request supervisor restart: %s", e)


class Bridge:
    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._supervisor_token: str = ""

    async def start(self):
        self._session = aiohttp.ClientSession()
        self._supervisor_token = os.environ.get("SUPERVISOR_TOKEN", "")
        if not self._supervisor_token:
            logger.error(
                "SUPERVISOR_TOKEN is not set — all HA sensor pushes will be skipped. "
                "Ensure hassio_api is enabled in config.yaml and the add-on is installed "
                "via Home Assistant Supervisor."
            )
        else:
            logger.info(
                "Bridge started — SUPERVISOR_TOKEN present (%s…).",
                self._supervisor_token[:10],
            )

    async def stop(self):
        if self._session:
            await self._session.close()
            self._session = None

    async def cleanup_legacy(self):
        if not self._session or not self._supervisor_token:
            return
        await cleanup_legacy_entities(self._session, self._supervisor_token)

    async def update(self, orders: list[dict], was_expired: bool = False):
        if not self._session or not self._supervisor_token:
            return
        await push_orders_sensor(self._session, orders, self._supervisor_token)
        if was_expired:
            await clear_token_expired_notification(self._session, self._supervisor_token)

    async def notify_token_expired(self):
        if not self._session or not self._supervisor_token:
            return
        await send_token_expired_notification(self._session, self._supervisor_token)

    async def fire_event(self, data: dict):
        if not self._session or not self._supervisor_token:
            return
        await fire_state_change_event(self._session, data, self._supervisor_token)

    async def restart(self):
        if not self._session or not self._supervisor_token:
            return
        await restart_addon(self._session, self._supervisor_token)

    async def register_card_resource(self):
        if not self._supervisor_token:
            return
        await register_lovelace_resource(self._supervisor_token)
