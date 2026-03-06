"""
poller.py — Polls GrabFood API for order status.
Sends cookies exactly as the browser does, not Authorization: Bearer.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

TOKEN_PATH = "/data/grab_token.json"
# onlyOngoingOrders=true returns active orders; fallback to false for last completed order
GRAB_ORDER_HISTORY_URL = (
    "https://food.grab.com/proxy/foodweb/v2/order/history"
    "?input.limit=1&input.offset=0&input.onlyOngoingOrders=true"
)
GRAB_ORDER_HISTORY_URL_FALLBACK = (
    "https://food.grab.com/proxy/foodweb/v2/order/history"
    "?input.limit=1&input.offset=0&input.onlyOngoingOrders=false"
)

FAST_STATES = {
    "FOOD_COLLECTED",
}

ACTIVE_STATES = {
    "ALLOCATING",
    "PICKING_UP",
    "DRIVER_AT_STORE",
}

IDLE_STATES = {
    "COMPLETED",
    "DRIVER_ARRIVED",
    "CANCELLED",
    "CANCELLED_PASSENGER",
    "CANCELLED_OPERATOR",
    "CANCELLED_DRIVER",
    "CANCELLED_MAX",
    "FAILED",
}

POLL_INTERVAL_FAST = 30       # FOOD_COLLECTED — driver en route, poll frequently
POLL_INTERVAL_ACTIVE = 60     # ALLOCATING, PICKING_UP, DRIVER_AT_STORE — 60s
POLL_INTERVAL_IDLE = 300      # COMPLETED, CANCELLED etc — 5 mins


def _load_session() -> Optional[dict]:
    """Load full session dict from disk."""
    try:
        with open(TOKEN_PATH) as f:
            saved = json.load(f)
            data = saved.get("data", {})
            if data.get("passenger_authn_token") and data.get("gfc_session"):
                logger.debug("Session loaded (saved at %s)", saved.get("updated_at", "?"))
                return data
            return None
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.warning("Could not read session file: %s", e)
        return None


def _extract_order_data(order: dict) -> dict:
    # Restaurant name
    restaurant = None
    try:
        restaurant = (
            order["snapshotDetail"]["cartWithQuote"]
            ["merchantCartWithQuoteList"][0]["merchantInfoObj"]["name"]
        )
    except (KeyError, IndexError, TypeError):
        pass

    # Driver location from driverTrack (driver field is always null)
    driver_track = order.get("driverTrack") or {}
    driver_lat = None
    driver_lon = None
    try:
        loc = driver_track.get("location", {})
        if loc.get("latitude") and loc.get("longitude"):
            driver_lat = loc["latitude"]
            driver_lon = loc["longitude"]
    except (KeyError, TypeError):
        pass

    # ETA absolute time from orderMeta
    eta = None
    try:
        raw_eta = order["orderMeta"]["expectedTime"]
        if raw_eta:
            if isinstance(raw_eta, (int, float)):
                eta = datetime.fromtimestamp(raw_eta, tz=timezone.utc).isoformat()
            else:
                eta = str(raw_eta)
    except (KeyError, TypeError):
        pass

    # ETA in minutes from driverTrack
    eta_minutes = None
    try:
        eta_minutes = driver_track.get("minETAInMin")
    except (KeyError, TypeError):
        pass

    state = order.get("orderState", "UNKNOWN")
    is_active = state in FAST_STATES or state in ACTIVE_STATES or state not in IDLE_STATES

    return {
        "order_status": state,
        "restaurant": restaurant,
        "driver_lat": driver_lat,
        "driver_lon": driver_lon,
        "eta": eta,
        "eta_minutes": eta_minutes,
        "active_order": is_active,
    }


async def _fetch_orders_from_url(
    session: aiohttp.ClientSession, sess_data: dict, url: str
) -> Optional[list]:
    """Internal: fetch orders list from a given URL. Returns None on error, [] if empty."""
    country = sess_data.get("country", "MY").upper()
    country_slug = country.lower()
    cookies = {
        "passenger_authn_token": sess_data["passenger_authn_token"],
        "gfc_session": sess_data["gfc_session"],
        "gfc_session_guid": sess_data.get("gfc_session_guid", ""),
    }
    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "en",
        "x-country-code": country,
        "x-gfc-country": country,
        "x-gfc-session": sess_data.get("session_key", ""),
        "user-agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "referer": f"https://food.grab.com/{country_slug}/en/order-history?support-deeplink=true&history=true",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }
    try:
        async with session.get(
            url,
            headers=headers,
            cookies=cookies,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status == 401:
                logger.warning("GrabFood API 401 — session expired, re-login needed.")
                return {"_token_expired": True}  # sentinel dict
            if resp.status != 200:
                text = await resp.text()
                logger.warning("GrabFood API HTTP %s: %s", resp.status, text[:200])
                return None
            body = await resp.json(content_type=None)
            response_block = (
                body.get("response")
                or (body.get("data") or {}).get("response")
                or {}
            )
            orders = response_block.get("orders", [])
            if not orders:
                logger.debug("Empty orders response. Top-level keys: %s", list(body.keys()))
            return orders
    except asyncio.TimeoutError:
        logger.warning("GrabFood API timed out.")
    except aiohttp.ClientError as e:
        logger.warning("GrabFood API client error: %s", e)
    except Exception as e:
        logger.exception("Unexpected error: %s", e)
    return None


async def fetch_latest_order(session: aiohttp.ClientSession, sess_data: dict) -> Optional[dict]:
    """Fetch latest order using cookies exactly like the browser does.

    Strategy:
      1. Try onlyOngoingOrders=true — returns the active order if one exists.
      2. If empty, fall back to onlyOngoingOrders=false — returns last completed order.
         This ensures we always have something to show in HA sensors.
    """
    # Step 1: try ongoing orders
    result = await _fetch_orders_from_url(session, sess_data, GRAB_ORDER_HISTORY_URL)

    # Propagate token-expired sentinel
    if isinstance(result, dict) and result.get("_token_expired"):
        return result

    if result:
        logger.debug("Ongoing order found via onlyOngoingOrders=true.")
        try:
            return _extract_order_data(result[0])
        except Exception as e:
            logger.exception("_extract_order_data failed on ongoing order: %s | raw: %s", e, str(result[0])[:500])
            return None

    # Step 2: no ongoing order — fall back to last order for sensor state
    logger.debug("No ongoing orders — falling back to order history.")
    await asyncio.sleep(3)  # avoid 429 rate limit
    result = await _fetch_orders_from_url(session, sess_data, GRAB_ORDER_HISTORY_URL_FALLBACK)

    if isinstance(result, dict) and result.get("_token_expired"):
        return result

    if not result:
        logger.debug("No orders in response.")
        return None

    try:
        return _extract_order_data(result[0])
    except Exception as e:
        logger.exception("_extract_order_data failed on fallback order: %s | raw: %s", e, str(result[0])[:500])
        return None


class GrabPoller:
    def __init__(self, on_update, on_token_expired):
        self._on_update = on_update
        self._on_token_expired = on_token_expired
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._latest: Optional[dict] = None
        self._token_expired = False

    @property
    def latest(self) -> Optional[dict]:
        return self._latest

    def start(self):
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self._poll_loop())
            logger.info("GrabPoller started.")

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            logger.info("GrabPoller stopped.")

    async def _poll_loop(self):
        async with aiohttp.ClientSession() as session:
            while self._running:
                sess_data = _load_session()
                if not sess_data:
                    logger.info("No session on disk yet — waiting 30s...")
                    await asyncio.sleep(30)
                    continue

                data = await fetch_latest_order(session, sess_data)

                if data and data.get("_token_expired"):
                    if not self._token_expired:
                        self._token_expired = True
                        await self._on_token_expired()
                    await asyncio.sleep(POLL_INTERVAL_IDLE)
                    continue

                was_expired = self._token_expired
                self._token_expired = False

                if data:
                    self._latest = data
                    try:
                        await self._on_update(data, was_expired=was_expired)
                    except Exception as e:
                        logger.exception("on_update error: %s", e)
                    state = data.get("order_status", "")
                    if state in FAST_STATES:
                        interval = POLL_INTERVAL_FAST
                    elif state in IDLE_STATES:
                        interval = POLL_INTERVAL_IDLE
                    else:
                        interval = POLL_INTERVAL_ACTIVE
                    logger.info("Order: %s | active=%s | next poll in %ss",
                        state, data.get("active_order"), interval)
                else:
                    interval = POLL_INTERVAL_IDLE

                await asyncio.sleep(interval)