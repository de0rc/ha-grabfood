# CLAUDE.md — ha-grabfood

## Project overview

Home Assistant add-on that uses a Playwright-controlled Chromium browser to log into GrabFood,
captures session cookies, then polls the GrabFood order history API for live order status and
pushes it as Home Assistant sensors.

## Architecture

| File | Role |
|------|------|
| `app/main.py` | aiohttp web server — HTTP routes, WebSocket VNC proxy, startup/shutdown |
| `app/browser.py` | Playwright browser automation — login UI, cookie capture, silent reauth |
| `app/poller.py` | `GrabPoller` async poll loop — calls GrabFood API, handles 401 / reauth |
| `app/bridge.py` | HA Supervisor API client — sensors, device_tracker, notifications, restart |
| `app/tokenstore.py` | Thread-safe session store — in-memory cache + `/data/grab_token.json` |
| `app/templates/index.html` | Jinja2 UI template |
| `Dockerfile` | Base image — Playwright/Chromium, noVNC, x11vnc, xvfb, Python deps |
| `config.yaml` | HA add-on manifest |
| `start.sh` | s6 service run script |

## Key conventions

- **All network I/O is async** (`aiohttp`, `asyncio`). Blocking disk/CPU ops use `asyncio.to_thread`.
- **Cookies passed manually per-request** — `DummyCookieJar` is used so `Set-Cookie` response headers are discarded instead of accumulating in memory. Never rely on the aiohttp session jar.
- **Session stored at `/data/grab_token.json`** — HA persistent storage, survives restarts. `TokenStore` keeps an in-memory copy; disk reads happen only on cold-start.
- **SUPERVISOR_TOKEN** from env grants access to the HA API (`http://supervisor/core/api`). If unset, all sensor pushes are silently skipped (logged at ERROR level).
- **SHOW_DRIVER_MAP** env var (`true`/`false`) toggles `device_tracker.grabfood_driver`.
- **Dep pinning** — all Python packages in `Dockerfile` are pinned for reproducibility. Update versions deliberately and test the build.
- **Poll intervals** — `FAST_STATES` (FOOD_COLLECTED, DRIVER_ARRIVED) → 30s, `ACTIVE_STATES` (ALLOCATING, PICKING_UP, DRIVER_AT_STORE) → 60s, `IDLE_STATES` (COMPLETED, CANCELLED, …) → 300s.
- **Post-login restart** — after a successful login or silent reauth, `bridge.restart()` asks the HA Supervisor to restart the add-on to reclaim memory held by Playwright. The session is saved to disk first so nothing is lost.

## Versioning & commit discipline

**Plan before committing.** Commits must be deliberate and complete — never partial, throwaway, or speculative. When in doubt, enter plan mode first.

Every commit **must** update both:
- `config.yaml` → `version: "0.1.x"` — this is what Home Assistant reads to detect updates
- `CHANGELOG.md` → add a `## 0.1.x` section at the top

Never bump one without the other. Version format is `0.1.x` in files (no leading `v`). Git commit messages use `v0.1.x` (e.g. `v0.1.16`).

## Build

```bash
docker build --build-arg BUILD_FROM=homeassistant/amd64-base:latest -t ha-grabfood .
```

Supported architectures: `amd64`, `aarch64` (set `BUILD_FROM` accordingly).

## GrabFood API

- **Endpoint:** `https://food.grab.com/proxy/foodweb/v2/order/history`
- **Auth:** cookies (`passenger_authn_token`, `gfc_session`, `gfc_session_guid`) + `x-gfc-session` header (extracted from `gfc_session` JWT payload as `sessionKey`)
- **Strategy:** try `onlyOngoingOrders=true` first; fall back to `false` for last completed order so sensors always have a value
- **401 handling:** raises `TokenExpiredError` → `GrabPoller` attempts `try_silent_reauth()` before alerting the user

## Sensors pushed to HA

| Entity | Value |
|--------|-------|
| `sensor.grabfood_order_status` | e.g. `Food Collected` |
| `sensor.grabfood_eta` | ISO 8601 timestamp |
| `sensor.grabfood_eta_minutes` | integer minutes |
| `sensor.grabfood_restaurant` | restaurant name string |
| `sensor.grabfood_active_order` | `on` / `off` |
| `device_tracker.grabfood_driver` | `home` (with lat/lon) or `not_home` |
