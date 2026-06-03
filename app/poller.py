"""
poller.py — Polls GrabFood API for order status.
Sends cookies exactly as the browser does, not Authorization: Bearer.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

import aiohttp

from browser import CHROME_USER_AGENT
from tokenstore import TokenStore

_LOGGER = logging.getLogger("grab.poller")

# Fetch all ongoing orders — no artificial limit so multiple simultaneous orders are captured.
# Fallback uses limit=1: we only need the single last completed order for sensor context.
GRAB_ORDER_HISTORY_URL = (
    "https://food.grab.com/proxy/foodweb/v2/order/history"
    "?input.limit=10&input.offset=0&input.onlyOngoingOrders=true"
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
    "UNKNOWN",  # no recognisable state — treat as idle, not active
}

POLL_INTERVAL_FAST = 30       # FOOD_COLLECTED, DRIVER_ARRIVED — poll frequently
POLL_INTERVAL_ACTIVE = 60     # ALLOCATING, PICKING_UP, DRIVER_AT_STORE — 60s
POLL_INTERVAL_IDLE = 300      # COMPLETED, CANCELLED etc — 5 mins
FALLBACK_RATE_LIMIT_DELAY = 3 # seconds between primary and fallback API requests

# Reauth-restart loop breaker: a successful silent reauth requests a supervisor restart to
# reclaim Playwright's memory. If the captured session is itself dead, that 401s again on the
# next boot → another reauth → another restart. These bound that loop.
MAX_REAUTH_RESTARTS = 3       # consecutive reauth-driven restarts allowed within the window
REAUTH_RESTART_WINDOW = 600   # seconds — reset the counter once this has elapsed

SESSION_RECREATE_INTERVAL = 6 * 3600  # recreate aiohttp session every 6 hours


class TokenExpiredError(Exception):
    """Raised when the GrabFood API returns 401 — session needs re-login."""
    pass


def _extract_order_data(order: dict) -> dict:
    # Order ID — try common field names used by the Grab API
    order_id = (
        order.get("orderID")
        or order.get("orderCode")
        or order.get("shortOrderNumber")
        or order.get("id")
    )

    # Restaurant name
    restaurant = None
    try:
        restaurant = (
            order["snapshotDetail"]["cartWithQuote"]
            ["merchantCartWithQuoteList"][0]["merchantInfoObj"]["name"]
        )
    except (KeyError, IndexError, TypeError):
        _LOGGER.debug("restaurant name not extractable from snapshotDetail — field may be absent")

    # Driver location from driverTrack (driver field is always null)
    driver_track = order.get("driverTrack") or {}
    driver_lat = None
    driver_lon = None
    try:
        loc = driver_track.get("location") or {}
        if loc.get("latitude") is not None and loc.get("longitude") is not None:
            driver_lat = loc["latitude"]
            driver_lon = loc["longitude"]
        else:
            _LOGGER.debug("driver lat/lon not available in driverTrack.location: %s", loc)
    except (KeyError, TypeError):
        _LOGGER.debug("driverTrack.location parse error")

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
            _LOGGER.debug("orderMeta.expectedTime is present but empty")
    except (KeyError, TypeError):
        _LOGGER.debug("eta not extractable from orderMeta — field may be absent")

    # ETA in minutes from driverTrack
    eta_minutes = None
    try:
        eta_minutes = driver_track.get("minETAInMin")
        if eta_minutes is None:
            _LOGGER.debug("driverTrack.minETAInMin not present")
    except (KeyError, TypeError):
        _LOGGER.debug("eta_minutes parse error from driverTrack")

    state = order.get("orderState")
    if not state:
        _LOGGER.warning(
            "orderState missing from order response — sensors will show unknown. "
            "Top-level order keys: %s", list(order.keys())
        )
        state = "UNKNOWN"

    # Only treat explicitly known active states as active — no catch-all
    is_active = state in FAST_STATES or state in ACTIVE_STATES

    return {
        "order_id": order_id,
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
    }
    gfc_guid = sess_data.get("gfc_session_guid")
    if gfc_guid:
        cookies["gfc_session_guid"] = gfc_guid
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
                _LOGGER.error(
                    "GrabFood API 401 at %s — session expired, attempting silent re-authentication.",
                    url
                )
                raise TokenExpiredError()
            if resp.status != 200:
                text = await resp.text()
                _LOGGER.warning(
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
                _LOGGER.debug(
                    "Empty orders response from %s. Top-level keys: %s",
                    url, list(body.keys())
                )
            return orders
    except TokenExpiredError:
        raise
    except asyncio.TimeoutError:
        _LOGGER.warning("GrabFood API timed out at %s.", url)
    except aiohttp.ClientError as e:
        _LOGGER.warning("GrabFood API client error at %s: %s", url, e)
    except Exception as e:
        _LOGGER.exception("Unexpected error fetching %s: %s", url, e)
    return None


async def fetch_orders(session: aiohttp.ClientSession, sess_data: dict) -> list[dict]:
    """Fetch all current orders using cookies exactly like the browser does.

    Strategy:
      1. Try onlyOngoingOrders=true — returns all active orders (no limit).
      2. If empty, fall back to onlyOngoingOrders=false (limit=1) — returns last
         completed order so sensors always have something to show.

    Returns a list of extracted order dicts (may be empty on network error).
    Raises TokenExpiredError if the session is expired.
    """
    # Step 1: all ongoing orders
    result = await _fetch_orders_from_url(session, sess_data, GRAB_ORDER_HISTORY_URL)

    if result:
        _LOGGER.debug("Fetched %d ongoing order(s).", len(result))
        orders = []
        for raw in result:
            try:
                orders.append(_extract_order_data(raw))
            except Exception as e:
                _LOGGER.exception("_extract_order_data failed: %s | raw: %s", e, str(raw)[:500])
        return orders

    # Step 2: no ongoing orders — fall back to last completed for sensor context
    _LOGGER.debug("No ongoing orders — falling back to order history.")
    await asyncio.sleep(FALLBACK_RATE_LIMIT_DELAY)
    result = await _fetch_orders_from_url(session, sess_data, GRAB_ORDER_HISTORY_URL_FALLBACK)

    if not result:
        _LOGGER.debug("No orders in fallback response.")
        return []

    try:
        return [_extract_order_data(result[0])]
    except Exception as e:
        _LOGGER.exception("_extract_order_data failed on fallback: %s | raw: %s", e, str(result[0])[:500])
        return []


class GrabPoller:
    def __init__(
        self,
        token_store: TokenStore,
        on_update: Callable[[list[dict]], Awaitable[None]],
        on_token_expired: Callable[[], Awaitable[None]],
        reauth: Callable[..., Awaitable[bool]],
        on_reauth_success: Optional[Callable[[], Awaitable[None]]] = None,
        on_recovered: Optional[Callable[[], Awaitable[None]]] = None,
        on_state_change: Optional[Callable[[dict], Awaitable[None]]] = None,
    ):
        self._token_store = token_store
        self._on_update = on_update
        self._on_token_expired = on_token_expired
        self._reauth = reauth
        self._on_reauth_success = on_reauth_success
        self._on_recovered = on_recovered
        self._on_state_change = on_state_change
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._latest: list[dict] = []
        self._last_states: dict[str, str] = {}  # order_id -> last known status
        self._wake = asyncio.Event()

        # Re-auth / notification state.
        self._notification_active = False   # an unresolved "re-login required" notification is shown
        self._reauth_suspended = False      # session is dead; stop relaunching Chromium until recovery
        self._reauth_restart_count = 0      # consecutive reauth-driven restarts (persisted)
        self._reauth_window_start = 0.0     # epoch seconds the current restart window began

    @property
    def latest(self) -> list[dict]:
        return self._latest

    def force_poll(self) -> None:
        """Wake the poll loop immediately, skipping the current sleep interval."""
        self._wake.set()

    def start(self):
        if not self._running:
            self._running = True
            last = self._token_store.load_order_sync()
            if last:
                # Handle both old single-dict format and new list format on disk
                orders = last if isinstance(last, list) else [last]
                self._latest = orders
                for order in orders:
                    oid = order.get("order_id") or "unknown"
                    self._last_states[oid] = order.get("order_status", "")
                _LOGGER.debug("Pre-loaded %d order(s) from disk.", len(orders))
            backoff = self._token_store.load_reauth_state_sync()
            self._reauth_restart_count = backoff.get("count", 0)
            self._reauth_window_start = backoff.get("window_start", 0.0)
            self._task = asyncio.create_task(self._poll_loop())
            _LOGGER.info("GrabPoller started.")

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            _LOGGER.info("GrabPoller stopped.")

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
                    _LOGGER.debug("aiohttp session recreated to free memory.")

                interval = await self._poll_once(session)

                # All waits are force_poll-interruptible so a fresh login / manual poll
                # request is picked up immediately instead of waiting out the interval.
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=interval)
                    self._wake.clear()
                    _LOGGER.debug("Poll woken early by force_poll().")
                except asyncio.TimeoutError:
                    pass
        finally:
            await session.close()

    async def _poll_once(self, session: aiohttp.ClientSession) -> int:
        """Run one poll cycle: fetch, push sensor, fire events. Returns seconds to wait next."""
        sess_data = await asyncio.to_thread(self._token_store.session_data_sync)
        if not sess_data:
            _LOGGER.info("No session on disk yet — waiting 30s...")
            return 30

        try:
            data = await fetch_orders(session, sess_data)
        except TokenExpiredError:
            return await self._handle_token_expired(sess_data)

        # Successful fetch — the session is valid, so clear any expired state.
        await self._on_recovery()

        if data:
            self._latest = data
            await asyncio.to_thread(self._token_store.save_order_sync, data)
            try:
                await self._on_update(data)
            except Exception as e:
                _LOGGER.exception("on_update error: %s", e)
            # Fire state-change events for any order whose status changed
            for order in data:
                oid = order.get("order_id") or "unknown"
                new_state = order.get("order_status", "")
                if self._last_states.get(oid) != new_state:
                    self._last_states[oid] = new_state
                    if self._on_state_change:
                        try:
                            await self._on_state_change(order)
                        except Exception as e:
                            _LOGGER.exception("on_state_change error: %s", e)
            # Poll interval driven by the most urgent active order
            statuses = {o.get("order_status", "") for o in data}
            if statuses & FAST_STATES:
                interval = POLL_INTERVAL_FAST
            elif statuses & ACTIVE_STATES:
                interval = POLL_INTERVAL_ACTIVE
            else:
                interval = POLL_INTERVAL_IDLE
            unknown = statuses - FAST_STATES - ACTIVE_STATES - IDLE_STATES - {""}
            if unknown:
                _LOGGER.warning("Unrecognised order state(s) — treating as idle: %s", unknown)
            _LOGGER.info(
                "%d order(s): %s | next poll in %ss",
                len(data), ", ".join(sorted(statuses)), interval
            )
            return interval

        return POLL_INTERVAL_IDLE

    async def _on_recovery(self) -> None:
        """Called after any successful fetch: clear expired-session state and dismiss notice."""
        if self._notification_active:
            self._notification_active = False
            if self._on_recovered:
                try:
                    await self._on_recovered()
                except Exception as e:
                    _LOGGER.exception("on_recovered error: %s", e)
        self._reauth_suspended = False
        if self._reauth_restart_count or self._reauth_window_start:
            self._reauth_restart_count = 0
            self._reauth_window_start = 0.0
            await asyncio.to_thread(
                self._token_store.save_reauth_state_sync, {"count": 0, "window_start": 0.0}
            )

    async def _handle_token_expired(self, sess_data: dict) -> int:
        """Handle a 401: attempt one silent reauth, validate it actually progressed, and
        either restart (to apply the new session) or notify — with a persisted loop breaker."""
        if self._reauth_suspended:
            # Already determined the session is dead — don't relaunch Chromium every cycle.
            return POLL_INTERVAL_IDLE

        old = (sess_data.get("passenger_authn_token"), sess_data.get("gfc_session"))
        ok = await self._reauth(on_token=self._token_store.save)

        if ok:
            new_data = await asyncio.to_thread(self._token_store.session_data_sync)
            new = (new_data.get("passenger_authn_token"), new_data.get("gfc_session"))
            if new == old:
                # Reauth re-captured identical (stale) cookies — the underlying session is
                # dead; restarting would just 401 again. Stop and ask for a manual re-login.
                _LOGGER.warning(
                    "Silent reauth captured identical cookies — session is dead, not restarting."
                )
                await self._mark_session_dead()
                return POLL_INTERVAL_IDLE

            if not await self._allow_reauth_restart():
                _LOGGER.error(
                    "Reauth-restart loop detected (%d within %ds) — giving up and notifying.",
                    self._reauth_restart_count, REAUTH_RESTART_WINDOW
                )
                await self._mark_session_dead()
                return POLL_INTERVAL_IDLE

            _LOGGER.info("Silent re-authentication succeeded — restarting to apply new session.")
            if self._on_reauth_success:
                await self._on_reauth_success()  # supervisor restart; process likely exits here
            return POLL_INTERVAL_IDLE

        # Reauth could not capture cookies — may be transient (Chromium error / logged-out
        # profile). Notify once but allow another attempt next cycle.
        _LOGGER.error("Silent re-authentication failed — manual re-login may be required.")
        if not self._notification_active:
            await self._on_token_expired()
            self._notification_active = True
        return POLL_INTERVAL_IDLE

    async def _allow_reauth_restart(self) -> bool:
        """Record a reauth-driven restart and return True if it's within the allowed budget."""
        now = time.time()
        if now - self._reauth_window_start > REAUTH_RESTART_WINDOW:
            self._reauth_window_start = now
            self._reauth_restart_count = 0
        self._reauth_restart_count += 1
        await asyncio.to_thread(
            self._token_store.save_reauth_state_sync,
            {"count": self._reauth_restart_count, "window_start": self._reauth_window_start},
        )
        return self._reauth_restart_count <= MAX_REAUTH_RESTARTS

    async def _mark_session_dead(self) -> None:
        """Suspend further silent reauth and notify the user once. Cleared on recovery."""
        self._reauth_suspended = True
        if not self._notification_active:
            await self._on_token_expired()
            self._notification_active = True
