# Changelog

## 0.1.5

### Improvements
- Start Xvfb and x11vnc on demand (only during login/reauth) instead of at add-on startup — eliminates idle RAM usage from persistent virtual display
- Display lifecycle now managed entirely by `browser.py`; removed from `start.sh`
- WebSocket VNC connection failures downgraded from `ERROR` to `DEBUG` — expected behaviour when no login is in progress
- Removed `check_vnc_port()` startup check — irrelevant with on-demand display

### Bug Fixes
- Silent reauth no longer creates a browser page or navigates to the login URL — cookies are read directly from the persistent context, avoiding an unnecessary page load
- `subprocess.Popen` no longer incorrectly wrapped in `asyncio.to_thread` — `Popen` forks immediately and is not a blocking call
- `_cleanup_browser_cache` now runs via `asyncio.to_thread` — `shutil.rmtree` is blocking disk I/O and should not run on the event loop
- VNC panel now shown only when status reaches `waiting_login`, not immediately on login click — prevents a failed WebSocket connection before the display is ready

## 0.1.4

### Bug Fixes
- Fix crash in `_extract_order_data` when `driverTrack.location` is `null` in API response (e.g. during `ALLOCATING` state before a driver is assigned)
- Add missing `log_level` translation entry for HA configuration UI

### Improvements
- Clear Chromium cache directories (`Cache`, `Code Cache`, `GPUCache`, `Service Worker`, `IndexedDB`) after each browser session to prevent unbounded profile growth
- Use `DummyCookieJar` for aiohttp session — discards `Set-Cookie` headers from API responses instead of accumulating them in memory
- Recreate aiohttp session every 6 hours to flush connection pool and internal state

## 0.1.3

### Improvements
- Automatic silent re-authentication on session expiry using saved browser profile
- On 401, the add-on now attempts to recapture cookies silently (30s timeout) before alerting the user
- HA notification and manual re-login only requested if silent reauth fails
- Web UI shows amber pulsing **Re-authenticating** badge during silent reauth attempt

## 0.1.2

### Bug Fixes
- Move `DRIVER_ARRIVED` from idle to fast poll states (30s interval)
- Replace token-expired sentinel dict with proper `TokenExpiredError` exception
- Fix `is_active` catch-all — now uses explicit known states only
- Fix race condition on login start with `asyncio.Lock`
- Fix fragile timestamp slicing in UI template — now formatted in Python

### Security
- Restrict x11vnc to `127.0.0.1` (was binding to `0.0.0.0`)

### Dependencies
- Pin `playwright==1.48.0`, `aiohttp==3.10.10`, `jinja2==3.1.4`
- Pin noVNC to `v1.5.0`, websockify to `v0.11.0`

### Code Quality
- Replace deprecated `asyncio.ensure_future` with `asyncio.create_task`
- Bump Chrome user-agent to 131, shared constant across `browser.py` and `poller.py`
- Inject `TokenStore` into `GrabPoller` instead of reading token file directly
- Wrap all blocking file I/O with `asyncio.to_thread`

### Logging
- Add `log_level` config option (`debug` / `info` / `warning` / `error`)
- Improved error visibility for supervisor token, login timeout, VNC, and API 401
- Debug logs for API response structure, sensor pushes, and driver map skip reasons

## 0.1.1

### Bug Fixes
- Fix `SUPERVISOR_TOKEN` not being injected — `start.sh` was called directly via `CMD` in Dockerfile, bypassing s6-overlay and `with-contenv`
- Fix manual token handler missing `country` and `gfc_session_guid` fields
- Fix `active_order` icon to `mdi:shopping`

### Code Quality
- Move `start.sh` to `/etc/services.d/grabfood/run` as s6 service
- Replace `Xvfb-run` with direct `Xvfb` and `x11vnc` background processes
- Read `SUPERVISOR_TOKEN` via `with-contenv` bashio shebang
- Replace deprecated `asyncio.ensure_future` with `create_task`
- Remove `__import__` hack, add proper `json` import

### Docs
- Add `DOCS.md` documentation page
- Add `translations/en.yaml` for config option labels and descriptions
- Add `DRIVER_ARRIVED` to README poll intervals table

## 0.1.0

- Browser-based GrabFood login via noVNC (no manual cookie extraction)
- Polls GrabFood API for live order status
- Exposes sensors: order status, ETA, ETA minutes, restaurant name, active order
- Optional driver location tracker (`device_tracker.grabfood_driver`)
- Auto-detects country from session (MY, SG, ID, etc.)
- Supports amd64 and aarch64 architectures
- Persistent session across add-on restarts