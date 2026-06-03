# Changelog

## 0.3.0

### Bug fixes

- **Map card never showed driver pins** — `bridge.py` stripped `driver_lat`/`driver_lon` from
  the `sensor.grabfood_orders` attributes, but the map card reads exactly those fields, so driver
  pins and route lines could never render. Coordinates are now included as raw numbers (or `null`),
  and the card filters with a finite-number check so a legitimate `0.0` latitude (e.g. on the
  equator) is no longer dropped as falsy.
- **Manual login could not cancel an in-progress silent reauth** — silent re-authentication is
  always poller-initiated, and the previous fix only cancelled UI-initiated tasks, so clicking
  "Login with Grab" during a reauth was a no-op that blocked the user for up to 60 s. Browser-session
  ownership is now centralised in a single `LoginManager`; a manual login cooperatively aborts the
  running reauth and proceeds immediately.
- **Token-expiry notification was never auto-dismissed** — the `was_expired` recovery signal was
  always false (the flag was reset before the next successful poll could read it), so the
  "Re-authentication Required" notification lingered forever after recovery. Recovery now uses a
  dedicated flag and dismisses the notification on the next successful poll.
- **Silent-reauth restart loop** — a successful reauth requested a supervisor restart to reclaim
  memory, but reauth declared success even when it re-captured the same dead cookies, so a truly
  expired session could loop: restart → 401 → reauth → restart. Reauth now verifies the captured
  cookies actually changed, and a persisted backoff caps reauth-driven restarts before falling back
  to a manual-relogin notification.
- **Manual session entry ignored country** — pasted cookies always defaulted to `MY`, breaking the
  API for other regions. The country is now read from the `gfc_session` token (falls back to `MY`).
- **base64 padding** — `extract_session_key` over-padded when the JWT payload length was already a
  multiple of 4.

### Code quality

- **`browser.py`** — module-global session state folded into a `LoginManager` class; cooperative
  abort (an `asyncio.Event` checked by the capture loop) replaces hard task cancellation.
- **`poller.py`** — per-cycle logic extracted into a testable `_poll_once` / `_handle_token_expired`;
  all poll waits are now `force_poll`-interruptible.
- **Map card** — `set hass` no longer rebuilds the DOM and refetches routes on every unrelated HA
  state change; it updates only when the tracked entity, the home zone, or dark mode changes.
- **`main.py`** — logs a warning when the `/config/grabfood_tracker` module override is active.
- **Tooling** — `check_version.py` now also verifies the built `grabfood-map-card.js`; `build_card.py
  --check` fails if the committed artifact is stale.
- **Tests** — added a `pytest` suite (`tests/`, `requirements-dev.txt`) covering the JWT helpers,
  order parsing, and the reauth/notification state machine, plus a CI workflow.

## 0.2.4

### Bug fixes

- **Manual login blocked by silent reauth fixed** — on startup with a stale token, the add-on automatically attempts a silent re-authentication. If the user clicked "Login with Grab" during this window, the server rejected it with "Login already in progress" and the UI showed a generic error. The manual login now cancels any in-progress silent reauth and proceeds immediately, so the user is never blocked waiting for the 60-second reauth timeout.

## 0.2.3

### Bug fixes

- **noVNC endless reconnect loop fixed** — `showVnc()` was called on every 2-second status poll while the browser was open, unconditionally resetting the iframe `src` and reloading the noVNC page each time. A `vncLive` flag now guards the `src` assignment so the iframe is only initialised once per login session; `hideVnc()` clears the flag when the browser is dismissed.
- **Login button permanently disabled after timeout fixed** — there was no way to abort an in-progress login, leaving the button stuck in a disabled/spinning state until the 180-second timeout elapsed (or after being caught in the reconnect loop). A **Cancel** button now appears alongside the spinner; clicking it immediately resets the UI and calls the new `POST /login/cancel` endpoint, which cancels the running browser task. The existing `finally` block in `launch_login` handles cleanup (Xvfb, x11vnc, state reset) on cancellation.

## 0.2.2

### Security

- **XSS fix in map card** — restaurant names, order status, and ETA values from the Grab API are now HTML-escaped before being interpolated into `innerHTML`. Prevents injection if the API ever returns HTML characters in those fields.

### Code quality

- **`bridge.py` refactored** — all module-level free functions folded into `Bridge` as private methods. Added `_headers` property to eliminate 5× copy-pasted header dict. `_register_lovelace_resource` now reuses `Bridge._session` instead of creating a rogue `ClientSession`. Logger standardised to `grab.bridge`.
- **`browser.py`** — added `_browser_lock` so the check-and-set of `_state["running"]` is atomic; prevents a race between a UI-triggered login and a poller-triggered silent reauth.
- **`poller.py`** — added type annotations to `GrabPoller.__init__`; extracted `FALLBACK_RATE_LIMIT_DELAY` constant; added warning log for unrecognised order states; logger standardised to `grab.poller`.
- **`tokenstore.py`** — `session_data_sync` disk-fallback path now logs a `WARNING` so it surfaces if the cold-start race ever fires.
- **`main.py`** — Jinja environment initialised in `on_startup` (stored in `app["jinja"]`) instead of a module-level lazy global; `handle_token_value` caches the token property; `on_token` callback now calls `poller.force_poll()` so the new session is picked up immediately after login without waiting for the next scheduled poll.
- **`index.html`** — removed Google Fonts CDN dependency; replaced with system font stack (`-apple-system` / `ui-monospace`).
- **Lovelace card** — JS template extracted to `app/www/grabfood-map-card.template.js`; `build_card.py` now assembles the artifact from the template file. Added `osrm_url` card config option to point at a self-hosted OSRM routing server.
- **`check_version.py`** — new root-level script that verifies `config.yaml`, `app/bridge.py`, and `app/www/grabfood-map-card.template.js` are all on the same version string.
- **`translations/en.yaml`** — removed stale `show_driver_map` entry (option was removed in v0.2.0).
- **`start.sh`** — removed dead `SHOW_DRIVER_MAP` read/export (option was removed in v0.2.0).

## 0.2.1

### Bug fixes

- **Map card sidebar overlap fixed** — on the HA companion app, opening the sidebar caused the
  map card to render above it. Fixed by adding `position: relative; z-index: 0` to `:host`,
  which creates a contained stacking context so Leaflet's internal z-indexes (up to 1000)
  no longer compete with the sidebar overlay in the outer document.

## 0.2.0

### Breaking changes

- **Sensor restructured** — the old per-field sensors (`sensor.grabfood_order_status`,
  `sensor.grabfood_eta`, `sensor.grabfood_eta_minutes`, `sensor.grabfood_restaurant`,
  `sensor.grabfood_active_order`, `sensor.grabfood_order_id`) no longer exist.
  All order data is in `sensor.grabfood_orders` — state is the active order count,
  `attributes.orders` is the full list of order objects.
  Stale entities from older versions are automatically removed from HA on startup.

- **`device_tracker.grabfood_driver` removed** — superseded by the map card, which
  renders driver positions directly without a separate entity. Removed automatically
  on startup. The `SHOW_DRIVER_MAP` option and environment variable are gone.

### Features

**Custom Lovelace map card (`grabfood-map-card`)**
- Leaflet map with CartoDB Voyager tiles; auto-registered as a Lovelace resource on
  startup with no manual resource step required.
- One driver pin per active order (teardrop + motorbike icon, distinct colour per order)
  plus a home pin in matching teardrop style.
- OSRM route polyline from each driver to home; routes cached per driver position.
- Fit-to-all button, automatic dark mode, `aspect_ratio` / `height` config options.
- Info header below the map showing restaurant name and ETA per active order.
  Shows "No active orders" when idle. Toggle with `show_info`.
- Visual editor (pencil icon) — configure without YAML.
- Versioned resource URL so updates are always picked up without a hard refresh.
- Appears in the Lovelace card picker.

**Multi-order support**
- `sensor.grabfood_orders` supports any number of simultaneous orders.
- Poll interval driven by the most urgent active order.
- Per-order `grabfood_order_state_changed` HA events, keyed by `order_id`.

**Sensor persistence**
- Cached orders pushed to HA immediately on startup — sensor is always available
  after a restart, not after the first poll interval.

---

## 0.1.16

### Security
- Fix path traversal in noVNC static file handler — `startswith(NOVNC_DIR)` lacked a
  separator boundary and could match adjacent directories; now uses `startswith(NOVNC_DIR + os.sep)`

### Bug Fixes
- Store `asyncio.create_task` result in `request.app["login_task"]` — untracked tasks
  have their exceptions silently discarded if they fail outside the broad inner except
- Fix token display in `/token/value` — `"..."` was unconditionally appended even when
  the token is shorter than 20 characters
- Fix silent reauth always failing — `context.cookies(url)` returns empty without a prior
  navigation to that origin; navigate to login URL first so cookies are immediately available
- Fix `_token_expired` flag never resetting after a failed silent reauth — a single failure
  permanently suppressed all future reauth attempts until the add-on was restarted
- Fix `_write_to_disk` not being atomic — token file could be corrupted if the process was
  killed mid-write; now uses temp file + `os.replace()`
- Clear Chromium `SingletonLock`/`SingletonCookie`/`SingletonSocket` before every launch —
  prevents profile load failures after an unclean shutdown

### Improvements
- Request a supervisor restart after every successful login or silent reauth —
  reclaims the memory Python's allocator retains after running Playwright.
  The session is saved to disk before the restart fires so nothing is lost.
- Start Xvfb and x11vnc on demand (only during login/reauth) instead of at add-on startup —
  eliminates idle RAM usage from persistent virtual display.
- Re-authentication notification includes a clickable **Open GrabFood Tracker** link.
- `tokenstore.session_data_sync`: reads from in-memory state first, falling back to disk
  only on cold-start edge cases.

### Dependencies
- Bump Chrome user-agent from 131 → 134
- Update pinned deps: `playwright 1.48.0→1.58.0`, `aiohttp 3.10.10→3.13.3`, `jinja2 3.1.4→3.1.6`

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
