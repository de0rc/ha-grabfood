# Changelog

## 0.1.16

### Security
- Fix path traversal in noVNC static file handler — `startswith(NOVNC_DIR)` lacked a
  separator boundary and could match adjacent directories; now uses `startswith(NOVNC_DIR + os.sep)`

### Bug Fixes
- Store `asyncio.create_task` result in `request.app["login_task"]` — untracked tasks
  have their exceptions silently discarded if they fail outside the broad inner except
- Fix token display in `/token/value` — `"..."` was unconditionally appended even when
  the token is shorter than 20 characters

### Code Quality
- Merge duplicate `not_home` state/attributes blocks in `push_driver_map` (`bridge.py`)
- Rename `_extract_session_key` → `extract_session_key` — private-by-convention name
  was being imported across module boundary in `main.py`

### Dependencies
- Bump Chrome user-agent from 131 → 134
- Update pinned deps: `playwright 1.48.0→1.58.0`, `aiohttp 3.10.10→3.13.3`, `jinja2 3.1.4→3.1.6`

## 0.1.15

### Improvements
- Request a supervisor restart after every successful login or silent reauth —
  reclaims the memory Python's allocator retains after running Playwright.
  The session is saved to disk before the restart fires so nothing is lost;
  the add-on is back up within a few seconds.
- `bridge.py`: `SUPERVISOR_TOKEN` and `SHOW_DRIVER_MAP` read once at startup
  and passed explicitly to each function instead of calling `os.environ.get`
  on every sensor push; removed unused `_notification_url()` helper.
- `tokenstore.session_data_sync`: reads from in-memory state first instead of
  disk on every poll cycle, falling back to disk only on cold-start edge cases.
- `main.py`: `login_lock` created inside `on_startup`; `NOVNC_DIR` uses
  `os.path.realpath`; `_extract_session_key` imported at module level;
  keepalive loop handles `CancelledError` cleanly on shutdown.
- `tokenstore.py`: removed unused `session_data` property.
- `DOCS.md`: added Memory Management section documenting the supervised restart behaviour.

## 0.1.9

### Bug Fixes
- Fix silent reauth always failing — `context.cookies(url)` returns empty without a prior
  navigation to that origin even when the profile has valid cookies on disk. Fix: navigate to the
  login URL before polling cookies; the authenticated profile loads instantly with no login prompt
  and cookies are immediately available
- Fix `_token_expired` flag never resetting after a failed silent reauth — a single failure
  permanently suppressed all future reauth attempts until the add-on was restarted
- Fix Chromium launch timeout crashing into outer exception handler — now exits cleanly via
  `context = None` guard
- Fix `_write_to_disk` not being atomic — token file could be corrupted if the process was killed
  mid-write; now uses temp file + `os.replace()`
- Clear Chromium `SingletonLock`/`SingletonCookie`/`SingletonSocket` before every launch —
  prevents profile load failures after an unclean shutdown

### Improvements
- Re-authentication notification now includes a clickable **Open GrabFood Tracker** link that
  navigates directly to the add-on web UI — no need to find it in the sidebar manually
- Increase `REAUTH_TIMEOUT` from 30s to 60s
- Replace deprecated `asyncio.get_event_loop()` with `asyncio.get_running_loop()`
- WebSocket upgrade, connect, and session-end logs downgraded from `INFO` to `DEBUG` — were
  firing every few seconds while the noVNC panel was open
- Silent reauth cookie capture log downgraded from `INFO` to `DEBUG`

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
- On 401, the add-on now attempts to recapture cookies silently before alerting the user
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