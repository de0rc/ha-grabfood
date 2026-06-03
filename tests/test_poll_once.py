"""State-machine tests for GrabPoller._poll_once / _handle_token_expired.

These cover the three highest-risk behaviours, with fakes for the browser and HTTP so no
Playwright or network is involved:
  - #3 token-expired notification is dismissed on recovery
  - #2 reauth that re-captures identical (dead) cookies does NOT restart
  - #2 reauth-restart loop is bounded by the persisted backoff
"""
import asyncio
from unittest.mock import AsyncMock

import pytest

import poller
from poller import GrabPoller, POLL_INTERVAL_FAST, POLL_INTERVAL_IDLE, MAX_REAUTH_RESTARTS
from tokenstore import TokenStore

OLD = {"passenger_authn_token": "OLD", "gfc_session": "OLDG", "session_key": "k", "country": "MY"}


@pytest.fixture
def store(tmp_path):
    return TokenStore(path=str(tmp_path / "grab_token.json"))


def _make_poller(store, reauth):
    return GrabPoller(
        token_store=store,
        on_update=AsyncMock(),
        on_token_expired=AsyncMock(),
        reauth=reauth,
        on_reauth_success=AsyncMock(),
        on_recovered=AsyncMock(),
        on_state_change=AsyncMock(),
    )


def _active_order(status="FOOD_COLLECTED"):
    return {"order_id": "O1", "order_status": status, "active_order": True,
            "restaurant": "R", "driver_lat": 1.0, "driver_lon": 2.0,
            "eta": None, "eta_minutes": 5}


async def test_no_session_waits_30(store):
    p = _make_poller(store, reauth=AsyncMock())
    assert await p._poll_once(None) == 30


async def test_successful_poll_pushes_and_sets_fast_interval(store, monkeypatch):
    await store.save(dict(OLD))
    monkeypatch.setattr(poller, "fetch_orders", AsyncMock(return_value=[_active_order()]))
    p = _make_poller(store, reauth=AsyncMock())

    interval = await p._poll_once(None)

    assert interval == POLL_INTERVAL_FAST
    p._on_update.assert_awaited_once()
    p._on_state_change.assert_awaited()  # status changed from nothing -> FOOD_COLLECTED


async def test_reauth_success_with_new_cookies_restarts(store, monkeypatch):
    await store.save(dict(OLD))
    monkeypatch.setattr(poller, "fetch_orders", AsyncMock(side_effect=poller.TokenExpiredError()))

    async def reauth(on_token):
        await on_token({**OLD, "passenger_authn_token": "FRESH", "gfc_session": "FRESHG"})
        return True

    p = _make_poller(store, reauth=reauth)
    interval = await p._poll_once(None)

    assert interval == POLL_INTERVAL_IDLE
    p._on_reauth_success.assert_awaited_once()   # restart requested
    p._on_token_expired.assert_not_awaited()      # no "please re-login" notification


async def test_reauth_identical_cookies_does_not_restart(store, monkeypatch):
    await store.save(dict(OLD))
    monkeypatch.setattr(poller, "fetch_orders", AsyncMock(side_effect=poller.TokenExpiredError()))

    async def reauth(on_token):
        await on_token(dict(OLD))  # re-captures the SAME dead cookies
        return True

    p = _make_poller(store, reauth=reauth)
    interval = await p._poll_once(None)

    assert interval == POLL_INTERVAL_IDLE
    p._on_reauth_success.assert_not_awaited()     # crucially: no restart loop
    p._on_token_expired.assert_awaited_once()     # user notified instead
    assert p._reauth_suspended is True


async def test_suspended_session_skips_reauth(store, monkeypatch):
    await store.save(dict(OLD))
    monkeypatch.setattr(poller, "fetch_orders", AsyncMock(side_effect=poller.TokenExpiredError()))
    reauth = AsyncMock(side_effect=AssertionError("reauth must not run while suspended"))
    p = _make_poller(store, reauth=reauth)
    p._reauth_suspended = True
    p._notification_active = True

    assert await p._poll_once(None) == POLL_INTERVAL_IDLE
    reauth.assert_not_awaited()


async def test_notification_dismissed_on_recovery(store, monkeypatch):
    await store.save(dict(OLD))
    p = _make_poller(store, reauth=AsyncMock(return_value=False))

    # First poll 401s and reauth fails -> notify.
    monkeypatch.setattr(poller, "fetch_orders", AsyncMock(side_effect=poller.TokenExpiredError()))
    await p._poll_once(None)
    p._on_token_expired.assert_awaited_once()
    assert p._notification_active is True

    # Next poll succeeds -> notification dismissed exactly once.
    monkeypatch.setattr(poller, "fetch_orders", AsyncMock(return_value=[_active_order()]))
    await p._poll_once(None)
    p._on_recovered.assert_awaited_once()
    assert p._notification_active is False


async def test_failed_reauth_notifies_only_once(store, monkeypatch):
    await store.save(dict(OLD))
    monkeypatch.setattr(poller, "fetch_orders", AsyncMock(side_effect=poller.TokenExpiredError()))
    p = _make_poller(store, reauth=AsyncMock(return_value=False))

    await p._poll_once(None)
    await p._poll_once(None)
    # Not suspended (failure may be transient), but the notification isn't re-sent each cycle.
    assert p._on_token_expired.await_count == 1


async def test_reauth_restart_loop_is_bounded(store, monkeypatch):
    await store.save(dict(OLD))
    monkeypatch.setattr(poller, "fetch_orders", AsyncMock(side_effect=poller.TokenExpiredError()))

    counter = {"n": 0}

    async def reauth(on_token):
        counter["n"] += 1
        # Always "succeeds" with DIFFERENT cookies each time (defeats the identical-cookie
        # guard) but the session stays dead — this is the rotating-cookie loop the backoff
        # counter exists to break.
        await on_token({**OLD, "passenger_authn_token": f"TOK{counter['n']}"})
        return True

    p = _make_poller(store, reauth=reauth)

    # The first MAX_REAUTH_RESTARTS 401s each trigger a restart.
    for _ in range(MAX_REAUTH_RESTARTS):
        await p._handle_token_expired(p._token_store.session_data_sync())
    assert p._on_reauth_success.await_count == MAX_REAUTH_RESTARTS
    assert p._reauth_suspended is False

    # The next one trips the breaker: no further restart, user notified, session suspended.
    await p._handle_token_expired(p._token_store.session_data_sync())
    assert p._on_reauth_success.await_count == MAX_REAUTH_RESTARTS  # unchanged
    assert p._reauth_suspended is True
    p._on_token_expired.assert_awaited_once()


async def test_backoff_persists_across_restart(store, monkeypatch):
    # Simulate the loop crossing a supervisor restart: the count is reloaded on start().
    store.save_reauth_state_sync({"count": MAX_REAUTH_RESTARTS, "window_start": 9e18})

    async def reauth(on_token):
        await on_token({**OLD, "passenger_authn_token": "DIFFERENT"})
        return True

    p = _make_poller(store, reauth=reauth)
    # Mimic start()'s backoff load without launching the real poll loop.
    backoff = store.load_reauth_state_sync()
    p._reauth_restart_count = backoff["count"]
    p._reauth_window_start = backoff["window_start"]

    await store.save(dict(OLD))
    await p._handle_token_expired(store.session_data_sync())

    # Already at the cap on boot -> this attempt must not restart again.
    p._on_reauth_success.assert_not_awaited()
    assert p._reauth_suspended is True
