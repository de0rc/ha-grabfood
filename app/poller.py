"""
poller.py — Polls GrabFood API for order status.
Sends cookies exactly as the browser does, not Authorization: Bearer.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import aiohttp

from browser import CHROME_USER_AGENT, try_silent_reauth
from tokenstore import TokenStore

logger = logging.getLogger(__name__)

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
    "DRIVER_ARRIVED",  # driver is at your door — poll frequently
}

ACTIVE_STATES = {
    "ALLOCATING",
    "PICKING_UP",
    "DRIVER_AT_STORE",
}

IDLE_STATES = {
    "COMPLETED",
    "CANCELLED",
    "CANCELLED_PASSENGER",
    "CANCELLED_OPERATOR",
    "CANCELLED_DRIVER",
    "CANCELLED_MAX",
    "FAILED",
}

POLL_INTERVAL_FAST = 30       # FOOD_COLLECTED, DRIVER_ARRIVED — poll frequently
POLL_INTERVAL_ACTIVE = 60     # ALLOCATING, PICKING_UP, DRIVER_AT_STORE — 60s
POLL_INTERVAL_IDLE = 300      # COMPLETED, CANCELLED etc — 5 mins


class TokenExpiredError(Exception):
    """Raised when the GrabFood API returns 401 — session needs re-login."""
    pass


def _extract_order_data(order: dict) -> dict:
    # Restaurant name
    restaurant = None
    try:
        restaurant = (
            order["snapshotDetail"]["cartWithQuote"]
            ["merchantCartWithQuoteList"][0]["merchantInfoObj"]["name"]
        )
    except (KeyError, IndexError, TypeError):
        logger.debug("restaurant name not extractable from snapshotDetail — field may be absent")

    # Driver location from driverTrack (driver field is always null)
    driver_track = order.get("driverTrack") or {}
    driver_lat = None
    driver_lon = None
    try:
        loc = driver_track.get("location") or {}
        if loc.get("latitude") and loc.get("longitude"):
            driver_lat = loc["latitude"]
            driver_lon = loc["longitude"]
        else:
            logger.debug("driver lat/lon not available in driverTrack.location: %s", loc)
    except (KeyError, TypeError):
        logger.debug("driverTrack.location parse error")

    # ETA absolute time from orderMeta
    eta = None
    try:
        raw_eta = order["orderMeta"]["expectedTime"]
        if raw_eta:
            if isinstance(raw_eta, (int, float)):
                eta = datetime.fromtimestamp(raw_eta, tz=timezone.utc).isoformat()
            else:
                eta = str(raw_eta)
        else:
            logger.debug("orderMeta.expectedTime is present but empty")
    except (KeyError, TypeError):
        logger.debug("eta not extractable from orderMeta — field may be absent")

    # ETA in minutes from driverTrack
    eta_minutes = None
    try:
        eta_minutes = driver_track.get("minETAInMin")
        if eta_minutes is None:
            logger.debug("driverTrack.minETAInMin not present")
    except (KeyError, TypeError):
        logger.debug("eta_minutes parse error from driverTrack")

    state = order.get("orderState")
    if not state:
        logger.warning(
            "orderState missing from order response — sensors will show unknown. "
            "Top-level order keys: %s", list(order.keys())
        )
        state = "UNKNOWN"

    # Only treat explicitly known active states as active — no catch-all
    is_active = state in FAST_STATES or state in ACTIVE_STATES

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
    """Internal: fetch orders list from a given URL.
    Returns None on network/parse error, [] if empty.
    Raises TokenExpiredError on 401.
    """
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
        "user-agent": CHROME_USER_AGENT,
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
                logger.error(
                    "GrabFood API 401 at %s — session expired, attempting silent re-authentication.",
                    url
                )
                raise TokenExpiredError()
            if resp.status != 200:
                text = await resp.text()
                logger.warning(
                    "GrabFood API HTTP %s at %s — response: %s",
                    resp.status, url, text[:500]
                )
                return None
            body = await resp.json(content_type=None)
            response_block = (
                body.get("response")
                or (body.get("data") or {}).get("response")
                or {}
            )
            orders = response_block.get("orders", [])
            if not orders:
                logger.debug(
                    "Empty orders response from %s. Top-level keys: %s",
                    url, list(body.keys())
                )
            return orders
    except TokenExpiredError:
        raise
    except asyncio.TimeoutError:
        logger.warning("GrabFood API timed out at %s.", url)
    except aiohttp.ClientError as e:
        logger.warning("GrabFood API client error at %s: %s", url, e)
    except Exception as e:
        logger.exception("Unexpected error fetching %s: %s", url, e)
    return None


async def fetch_latest_order(session: aiohttp.ClientSession, sess_data: dict) -> Optional[dict]:
    """Fetch latest order using cookies exactly like the browser does.

    Strategy:
      1. Try onlyOngoingOrders=true — returns the active order if one exists.
      2. If empty, fall back to onlyOngoingOrders=false — returns last completed order.
         This ensures we always have something to show in HA sensors.

    Raises TokenExpiredError if the session is expired.
    """
    # Step 1: try ongoing orders
    result = await _fetch_orders_from_url(session, sess_data, GRAB_ORDER_HISTORY_URL)

    if result:
        logger.debug("Ongoing order found via onlyOngoingOrders=true.")
        try:
            return _extract_order_data(result[0])
        except Exception as e:
            logger.exception(
                "_extract_order_data failed on ongoing order: %s | raw: %s",
                e, str(result[0])[:500]
            )
            return None

    # Step 2: no ongoing order — fall back to last order for sensor state
    logger.debug("No ongoing orders — falling back to order history.")
    await asyncio.sleep(3)  # avoid 429 rate limit
    result = await _fetch_orders_from_url(session, sess_data, GRAB_ORDER_HISTORY_URL_FALLBACK)

    if not result:
        logger.debug("No orders in fallback response.")
        return None

    try:
        return _extract_order_data(result[0])
    except Exception as e:
        logger.exception(
            "_extract_order_data failed on fallback order: %s | raw: %s",
            e, str(result[0])[:500]
        )
        return None


SESSION_RECREATE_INTERVAL = 6 * 3600  # recreate aiohttp session every 6 hours


class GrabPoller:
    def __init__(self, token_store: TokenStore, on_update, on_token_expired):
        self._token_store = token_store
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

    def _new_session(self) -> aiohttp.ClientSession:
        """Create a new aiohttp session with DummyCookieJar.
        Cookies are passed manually per-request so we don't need the jar —
        DummyCookieJar discards all Set-Cookie headers, preventing accumulation in memory.
        """
        return aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar())

    async def _poll_loop(self):
        session = self._new_session()
        session_created_at = asyncio.get_running_loop().time()

        try:
            while self._running:
                # Recreate session every 6 hours to flush connection pool and internal state
                if asyncio.get_running_loop().time() - session_created_at >= SESSION_RECREATE_INTERVAL:
                    await session.close()
                    session = self._new_session()
                    session_created_at = asyncio.get_running_loop().time()
                    logger.debug("aiohttp session recreated to free memory.")

                sess_data = await asyncio.to_thread(self._token_store.session_data_sync)
                if not sess_data:
                    logger.info("No session on disk yet — waiting 30s...")
                    await asyncio.sleep(30)
                    continue

                try:
                    data = await fetch_latest_order(session, sess_data)
                except TokenExpiredError:
                    if not self._token_expired:
                        self._token_expired = True
                        # Attempt silent re-authentication before alerting the user
                        reauth_success = await try_silent_reauth(
                            on_token=self._token_store.save
                        )
                        if reauth_success:
                            logger.info(
                                "Silent re-authentication succeeded — resuming polling."
                            )
                            self._token_expired = False
                        else:
                            logger.error(
                                "Silent re-authentication failed — manual re-login required. "
                                "HA notification sent."
                            )
                            await self._on_token_expired()
                            # Reset flag so reauth is attempted again next cycle rather
                            # than being skipped indefinitely after a single failure.
                            self._token_expired = False
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
                    logger.info(
                        "Order: %s | active=%s | next poll in %ss",
                        state, data.get("active_order"), interval
                    )
                else:
                    interval = POLL_INTERVAL_IDLE

                await asyncio.sleep(interval)
        finally:
            await session.close()