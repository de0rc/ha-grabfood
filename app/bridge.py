"""
bridge.py — Pushes GrabFood order data to Home Assistant as sensors.
"""

import logging
import os
from typing import Any, Optional

import aiohttp

_LOGGER = logging.getLogger("grab.bridge")

HA_API_BASE = "http://supervisor/core/api"
HA_WS_URL = "ws://supervisor/core/websocket"
SENSOR_PREFIX = "sensor.grabfood"

ORDERS_ENTITY = f"{SENSOR_PREFIX}_orders"

# Must match version in config.yaml and app/www/grabfood-map-card.template.js _VERSION constant.
ADDON_VERSION = "0.2.4"
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


class Bridge:
    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._supervisor_token: str = ""

    @property
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._supervisor_token}",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self):
        self._session = aiohttp.ClientSession()
        self._supervisor_token = os.environ.get("SUPERVISOR_TOKEN", "")
        if not self._supervisor_token:
            _LOGGER.error(
                "SUPERVISOR_TOKEN is not set — all HA sensor pushes will be skipped. "
                "Ensure hassio_api is enabled in config.yaml and the add-on is installed "
                "via Home Assistant Supervisor."
            )
        else:
            _LOGGER.info(
                "Bridge started — SUPERVISOR_TOKEN present (%s…).",
                self._supervisor_token[:10],
            )

    async def stop(self):
        if self._session:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def cleanup_legacy(self):
        if not self._session or not self._supervisor_token:
            return
        await self._cleanup_legacy()

    async def update(self, orders: list[dict], was_expired: bool = False):
        if not self._session or not self._supervisor_token:
            return
        await self._push_orders_sensor(orders)
        if was_expired:
            await self._clear_token_expired_notification()

    async def notify_token_expired(self):
        if not self._session or not self._supervisor_token:
            return
        await self._send_token_expired_notification()

    async def fire_event(self, data: dict):
        if not self._session or not self._supervisor_token:
            return
        await self._fire_state_change_event(data)

    async def restart(self):
        if not self._session or not self._supervisor_token:
            return
        await self._restart_addon()

    async def register_card_resource(self):
        if not self._session or not self._supervisor_token:
            return
        await self._register_lovelace_resource()

    # ------------------------------------------------------------------
    # Private implementation
    # ------------------------------------------------------------------

    async def _cleanup_legacy(self) -> None:
        """Delete entities created by older add-on versions that no longer exist."""
        removed = []
        for entity_id in _LEGACY_ENTITIES:
            try:
                async with self._session.delete(
                    f"{HA_API_BASE}/states/{entity_id}",
                    headers=self._headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        removed.append(entity_id)
                    elif resp.status != 404:
                        text = await resp.text()
                        _LOGGER.warning(
                            "Unexpected response deleting %s: HTTP %s — %s",
                            entity_id, resp.status, text[:200],
                        )
            except Exception as e:
                _LOGGER.warning("Error deleting legacy entity %s: %s", entity_id, e)

        if removed:
            _LOGGER.info("Removed %d legacy entity/entities: %s", len(removed), ", ".join(removed))
        else:
            _LOGGER.debug("No legacy entities found to remove.")

    async def _push_orders_sensor(self, orders: list[dict]) -> None:
        """Push sensor.grabfood_orders — state is active order count, attributes hold full order list."""
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
            async with self._session.post(
                f"{HA_API_BASE}/states/{ORDERS_ENTITY}",
                json=payload,
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status not in (200, 201):
                    text = await resp.text()
                    _LOGGER.warning("Failed to push orders sensor: HTTP %s — %s", resp.status, text[:200])
                else:
                    _LOGGER.info(
                        "Pushed sensor.grabfood_orders — %d order(s), %d active.",
                        len(orders), active_count,
                    )
        except Exception as e:
            _LOGGER.warning("Error pushing orders sensor: %s", e)

    async def _send_token_expired_notification(self) -> None:
        payload = {
            "title": "GrabFood Tracker — Re-authentication Required",
            "message": (
                "Your GrabFood session has expired and could not be renewed automatically.\n\n"
                "[Open GrabFood Tracker](/hassio/ingress/grabfood_tracker) and log in again."
            ),
            "notification_id": "grabfood_tracker_token_expired",
        }
        try:
            async with self._session.post(
                f"{HA_API_BASE}/services/persistent_notification/create",
                json=payload,
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    _LOGGER.info("Token expiry notification sent to HA.")
                else:
                    text = await resp.text()
                    _LOGGER.warning(
                        "Failed to send HA notification: HTTP %s — %s", resp.status, text[:200],
                    )
        except Exception as e:
            _LOGGER.warning("Error sending HA notification: %s", e)

    async def _clear_token_expired_notification(self) -> None:
        try:
            async with self._session.post(
                f"{HA_API_BASE}/services/persistent_notification/dismiss",
                json={"notification_id": "grabfood_tracker_token_expired"},
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    _LOGGER.debug("Token expiry notification dismissed.")
        except Exception as e:
            _LOGGER.debug("Could not dismiss notification: %s", e)

    async def _fire_state_change_event(self, data: dict) -> None:
        """Fire grabfood_order_state_changed HA event when order status transitions."""
        payload = {
            "order_status": data.get("order_status"),
            "order_id": data.get("order_id"),
            "restaurant": data.get("restaurant"),
            "active_order": data.get("active_order"),
        }
        try:
            async with self._session.post(
                f"{HA_API_BASE}/events/grabfood_order_state_changed",
                json=payload,
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    _LOGGER.debug(
                        "Fired grabfood_order_state_changed: %s", data.get("order_status"),
                    )
                else:
                    text = await resp.text()
                    _LOGGER.warning(
                        "Failed to fire state change event: HTTP %s — %s", resp.status, text[:200],
                    )
        except Exception as e:
            _LOGGER.warning("Error firing state change event: %s", e)

    async def _restart_addon(self) -> None:
        """Ask the HA supervisor to restart this add-on.

        Called after a successful login or silent reauth to reclaim the memory that
        Python's allocator retains after running Playwright. The session is already
        saved to disk before this is called so nothing is lost.
        """
        _LOGGER.info("Requesting supervisor restart to reclaim memory after browser session...")
        try:
            async with self._session.post(
                "http://supervisor/addons/self/restart",
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    _LOGGER.info("Supervisor restart requested successfully.")
                else:
                    text = await resp.text()
                    _LOGGER.warning(
                        "Supervisor restart request failed: HTTP %s — %s", resp.status, text[:200],
                    )
        except Exception as e:
            _LOGGER.warning("Could not request supervisor restart: %s", e)

    async def _register_lovelace_resource(self) -> None:
        """Register/update grabfood-map-card.js Lovelace resource with versioned URL.

        Uses a versioned query string (?v=X.Y.Z) so HA's service worker treats each
        add-on version as a new resource and always fetches it fresh from disk.
        On each startup: finds any existing grabfood-map-card.js entry (any version),
        updates it to the current versioned URL, or creates it if missing.
        """
        try:
            async with self._session.ws_connect(HA_WS_URL) as ws:
                msg = await ws.receive_json()
                if msg.get("type") != "auth_required":
                    raise RuntimeError(f"Unexpected first WS message: {msg}")
                await ws.send_json({"type": "auth", "access_token": self._supervisor_token})
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
                    _LOGGER.debug("Lovelace resource already at current version: %s", CARD_URL)
                    return

                if existing_entry:
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
                    _LOGGER.info("Updated Lovelace resource to: %s", CARD_URL)
                else:
                    await ws.send_json({
                        "id": 2,
                        "type": "lovelace/resources/create",
                        "res_type": "module",
                        "url": CARD_URL,
                    })
                    msg = await ws.receive_json()
                    if not msg.get("success"):
                        raise RuntimeError(f"Create failed: {msg}")
                    _LOGGER.info("Registered Lovelace resource: %s", CARD_URL)
        except Exception as e:
            _LOGGER.warning("Could not register Lovelace resource via WS: %s", e)
