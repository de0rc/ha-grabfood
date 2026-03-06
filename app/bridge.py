"""
bridge.py — Pushes GrabFood order data to Home Assistant as sensors.
Also optionally pushes a device_tracker for the driver map (SHOW_DRIVER_MAP=true).
"""

import logging
import os
from typing import Any, Optional

import aiohttp

logger = logging.getLogger(__name__)

HA_API_BASE = "http://supervisor/core/api"
SENSOR_PREFIX = "sensor.grabfood"
DEVICE_TRACKER_ENTITY = "device_tracker.grabfood_driver"


def _supervisor_token() -> str:
    return os.environ.get("SUPERVISOR_TOKEN", "")


def _show_driver_map() -> bool:
    return os.environ.get("SHOW_DRIVER_MAP", "false").lower() == "true"


def _auth_headers() -> dict:
    return {
        "Authorization": f"Bearer {_supervisor_token()}",
        "Content-Type": "application/json",
    }


def _sensor_url(suffix: str) -> str:
    return f"{HA_API_BASE}/states/{SENSOR_PREFIX}_{suffix}"


def _notification_url() -> str:
    return f"{HA_API_BASE}/services/persistent_notification/create"


SENSOR_MAP = {
    "order_status": ("order_status", "Order Status",  None),
    "eta":          ("eta",          "ETA",           None),
    "eta_minutes":  ("eta_minutes",  "ETA Minutes",   "min"),
    "restaurant":   ("restaurant",   "Restaurant",    None),
    "active_order": ("active_order", "Active Order",  None),
}


async def push_sensors(session: aiohttp.ClientSession, data: dict) -> None:
    if not _supervisor_token():
        logger.warning("SUPERVISOR_TOKEN not set — cannot push to HA.")
        return

    for data_key, (suffix, friendly_name, unit) in SENSOR_MAP.items():
        value = data.get(data_key)

        if isinstance(value, bool):
            state = "on" if value else "off"
        elif value is None:
            state = "unknown"
        elif data_key == "order_status":
            state = str(value).replace("_", " ").title()
        else:
            state = str(value)

        payload: dict[str, Any] = {
            "state": state,
            "attributes": {
                "friendly_name": friendly_name,
                "icon": _icon_for(suffix),
            },
        }
        if unit:
            payload["attributes"]["unit_of_measurement"] = unit

        try:
            async with session.post(
                _sensor_url(suffix),
                json=payload,
                headers=_auth_headers(),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status not in (200, 201):
                    text = await resp.text()
                    logger.warning("Failed to set %s: HTTP %s — %s", suffix, resp.status, text[:200])
                else:
                    logger.debug("Set %s = %s", suffix, state)
        except Exception as e:
            logger.warning("Error pushing sensor %s: %s", suffix, e)


async def push_driver_map(session: aiohttp.ClientSession, data: dict) -> None:
    """Push device_tracker.grabfood_driver with lat/lon so it shows on HA map."""
    if not _supervisor_token():
        return

    lat = data.get("driver_lat")
    lon = data.get("driver_lon")
    active = data.get("active_order", False)

    if not active or lat is None or lon is None:
        state = "not_home"
        attributes: dict[str, Any] = {
            "friendly_name": "GrabFood Driver",
            "icon": "mdi:moped",
        }
    else:
        state = "home"
        attributes = {
            "friendly_name": "GrabFood Driver",
            "icon": "mdi:moped",
            "latitude": lat,
            "longitude": lon,
            "gps_accuracy": 10,
            "source_type": "gps",
        }

    try:
        async with session.post(
            f"{HA_API_BASE}/states/{DEVICE_TRACKER_ENTITY}",
            json={"state": state, "attributes": attributes},
            headers=_auth_headers(),
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status not in (200, 201):
                text = await resp.text()
                logger.warning("Failed to update driver tracker: HTTP %s — %s", resp.status, text[:200])
            else:
                logger.debug("Driver tracker: lat=%s lon=%s", lat, lon)
    except Exception as e:
        logger.warning("Error pushing driver tracker: %s", e)


async def send_token_expired_notification(session: aiohttp.ClientSession) -> None:
    if not _supervisor_token():
        return

    payload = {
        "title": "GrabFood Tracker — Re-authentication Required",
        "message": (
            "Your GrabFood session has expired. "
            "Please open the **GrabFood Tracker** panel in the sidebar and log in again."
        ),
        "notification_id": "grabfood_tracker_token_expired",
    }
    try:
        async with session.post(
            _notification_url(),
            json=payload,
            headers=_auth_headers(),
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                logger.info("Token expiry notification sent to HA.")
            else:
                text = await resp.text()
                logger.warning("Failed to send HA notification: HTTP %s — %s", resp.status, text[:200])
    except Exception as e:
        logger.warning("Error sending HA notification: %s", e)


async def clear_token_expired_notification(session: aiohttp.ClientSession) -> None:
    if not _supervisor_token():
        return

    try:
        async with session.post(
            f"{HA_API_BASE}/services/persistent_notification/dismiss",
            json={"notification_id": "grabfood_tracker_token_expired"},
            headers=_auth_headers(),
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                logger.debug("Token expiry notification dismissed.")
    except Exception as e:
        logger.debug("Could not dismiss notification: %s", e)


def _icon_for(suffix: str) -> str:
    icons = {
        "order_status": "mdi:food-takeout-box",
        "eta":          "mdi:clock-outline",
        "eta_minutes":  "mdi:timer-outline",
        "restaurant":   "mdi:store",
        "active_order": "mdi:shopping",
    }
    return icons.get(suffix, "mdi:information-outline")


class Bridge:
    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None

    async def start(self):
        self._session = aiohttp.ClientSession()
        logger.info("Bridge started (driver_map=%s).", _show_driver_map())

    async def stop(self):
        if self._session:
            await self._session.close()
            self._session = None

    async def update(self, data: dict, was_expired: bool = False):
        if not self._session:
            return
        await push_sensors(self._session, data)
        if _show_driver_map():
            await push_driver_map(self._session, data)
        if was_expired:
            await clear_token_expired_notification(self._session)

    async def notify_token_expired(self):
        if not self._session:
            return
        await send_token_expired_notification(self._session)