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


SENSOR_MAP = {
    "order_status": ("order_status", "Order Status",  None),
    "eta":          ("eta",          "ETA",           None),
    "eta_minutes":  ("eta_minutes",  "ETA Minutes",   "min"),
    "restaurant":   ("restaurant",   "Restaurant",    None),
    "active_order": ("active_order", "Active Order",  None),
}


async def push_sensors(session: aiohttp.ClientSession, data: dict, supervisor_token: str) -> None:
    headers = {
        "Authorization": f"Bearer {supervisor_token}",
        "Content-Type": "application/json",
    }
    pushed = 0
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
                f"{HA_API_BASE}/states/{SENSOR_PREFIX}_{suffix}",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status not in (200, 201):
                    text = await resp.text()
                    logger.warning(
                        "Failed to set %s: HTTP %s — %s", suffix, resp.status, text[:200]
                    )
                else:
                    pushed += 1
                    logger.debug("Set %s = %s", suffix, state)
        except Exception as e:
            logger.warning("Error pushing sensor %s: %s", suffix, e)

    if pushed:
        logger.info("Pushed %d/%d sensors to HA.", pushed, len(SENSOR_MAP))


async def push_driver_map(session: aiohttp.ClientSession, data: dict, supervisor_token: str) -> None:
    """Push device_tracker.grabfood_driver with lat/lon so it shows on HA map."""
    headers = {
        "Authorization": f"Bearer {supervisor_token}",
        "Content-Type": "application/json",
    }

    lat = data.get("driver_lat")
    lon = data.get("driver_lon")
    active = data.get("active_order", False)

    if not active or lat is None or lon is None:
        if not active:
            logger.debug("Driver map push skipped — no active order.")
        else:
            logger.debug("Driver map push skipped — lat/lon not yet available (driver not assigned?).")
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
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status not in (200, 201):
                text = await resp.text()
                logger.warning(
                    "Failed to update driver tracker: HTTP %s — %s", resp.status, text[:200]
                )
            else:
                logger.debug("Driver tracker updated: lat=%s lon=%s", lat, lon)
    except Exception as e:
        logger.warning("Error pushing driver tracker: %s", e)


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


def _icon_for(suffix: str) -> str:
    icons = {
        "order_status": "mdi:food-takeout-box",
        "eta":          "mdi:clock-outline",
        "eta_minutes":  "mdi:timer-outline",
        "restaurant":   "mdi:store",
        "active_order": "mdi:shopping",
    }
    return icons.get(suffix, "mdi:information-outline")


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
        self._show_driver_map: bool = False

    async def start(self):
        self._session = aiohttp.ClientSession()
        self._supervisor_token = os.environ.get("SUPERVISOR_TOKEN", "")
        self._show_driver_map = os.environ.get("SHOW_DRIVER_MAP", "false").lower() == "true"
        if not self._supervisor_token:
            logger.error(
                "SUPERVISOR_TOKEN is not set — all HA sensor pushes will be skipped. "
                "Ensure hassio_api is enabled in config.yaml and the add-on is installed "
                "via Home Assistant Supervisor."
            )
        else:
            logger.info(
                "Bridge started — SUPERVISOR_TOKEN present (%s…), driver_map=%s.",
                self._supervisor_token[:10], self._show_driver_map
            )

    async def stop(self):
        if self._session:
            await self._session.close()
            self._session = None

    async def update(self, data: dict, was_expired: bool = False):
        if not self._session or not self._supervisor_token:
            return
        await push_sensors(self._session, data, self._supervisor_token)
        if self._show_driver_map:
            await push_driver_map(self._session, data, self._supervisor_token)
        else:
            logger.debug("Driver map push skipped — show_driver_map is disabled.")
        if was_expired:
            await clear_token_expired_notification(self._session, self._supervisor_token)

    async def notify_token_expired(self):
        if not self._session or not self._supervisor_token:
            return
        await send_token_expired_notification(self._session, self._supervisor_token)

    async def restart(self):
        if not self._session or not self._supervisor_token:
            return
        await restart_addon(self._session, self._supervisor_token)