# GrabFood Tracker

## First Time Setup

1. Start the add-on and open the Web UI from the sidebar.
2. Click **Login with Grab** — a browser session will start and appear via noVNC.
3. Log in to your GrabFood account normally.
4. The session will be captured automatically — the browser will close once done.
5. Your sensors will start updating within 60 seconds.

### Alternative: Manual Session Entry

If the browser login flow doesn't work in your environment, you can enter session cookies manually:

1. Open your browser's developer tools on [food.grab.com](https://food.grab.com) and log in.
2. From the **Application → Cookies** tab, copy the values for `passenger_authn_token` and `gfc_session`.
3. Open the Web UI, expand **Manual session entry** at the bottom, paste both values, and click **Save Session**.

## Configuration

### `show_driver_map` (default: `false`)

Enable this to track your delivery driver's location on the Home Assistant map.

When enabled, a `device_tracker.grabfood_driver` entity will be created and updated with the driver's GPS coordinates in real time. You can add this to a Map card in your dashboard.

### `log_level` (default: `info`)

Controls the verbosity of add-on logs. Accepted values: `debug`, `info`, `warning`, `error`.

Use `debug` when troubleshooting — it enables detailed logs for API responses, sensor pushes, cookie capture, and driver location updates. Switch back to `info` for normal use to keep logs clean.

## Sensors

| Entity | Description |
|--------|-------------|
| `sensor.grabfood_order_status` | Current order state (e.g. Allocating, Food Collected, Completed) |
| `sensor.grabfood_eta` | Estimated delivery time (ISO timestamp) |
| `sensor.grabfood_eta_minutes` | Minutes until delivery |
| `sensor.grabfood_restaurant` | Restaurant name |
| `sensor.grabfood_active_order` | `on` when an order is active, `off` otherwise |

## Order States

| State | Meaning | Poll Interval |
|-------|---------|---------------|
| `Allocating` | Finding a driver | 60 seconds |
| `Picking Up` | Driver heading to restaurant | 60 seconds |
| `Driver At Store` | Driver at the restaurant | 60 seconds |
| `Food Collected` | Driver heading to you | 30 seconds |
| `Driver Arrived` | Driver at your door | 30 seconds |
| `Completed` | Order delivered | 5 minutes |
| `Cancelled` | Order cancelled | 5 minutes |

## Session Expiry

When a session expires, the add-on will first attempt to re-authenticate silently using the saved browser profile — no action needed from you. If valid cookies are still present in the profile, the session will be restored automatically within 30 seconds and polling will resume without interruption.

During this attempt the Web UI will show an amber **Re-authenticating** badge.

If the silent attempt fails (profile cookies are also expired), you will receive a persistent notification in Home Assistant asking you to log in again. Open the Web UI from the sidebar and click **Start Login** to complete the process.

The `sensor.grabfood_active_order` sensor will remain in its last known state until the session is restored.

## Supported Countries

This add-on works with any country where GrabFood is available. The country is detected automatically from your GrabFood account when you log in.

## Troubleshooting

**Sensors not updating after session expiry, HA notification received**
The add-on attempted silent re-authentication but the saved browser profile had no valid cookies. Open the Web UI, click **Start Login**, and log in to your GrabFood account. Polling will resume automatically once the session is captured.

**Sensors not updating**
Check that you are logged in via the Web UI. If the session has expired, a notification will appear in Home Assistant — log in again to resume tracking.

**Sensors never populate / all unknown after login**
Check the add-on logs for `SUPERVISOR_TOKEN is not set`. This means the add-on cannot reach the Home Assistant API. Ensure the add-on is installed via the Home Assistant Supervisor (not bare Docker), and that `hassio_api: true` is present in the add-on configuration.

**noVNC screen is black or not appearing**
The browser session starts on demand — the noVNC panel will appear automatically once the display is ready (a few seconds after clicking **Login with Grab**). If the screen remains black after the panel appears, wait a moment and refresh.

**Login timed out**
The browser session window closed before you completed login. Click **Start Login** again — you have 3 minutes to complete the login process.

**session_key extraction warning in logs**
This means the `gfc_session` cookie was captured but its JWT payload could not be decoded. API calls may fail. Try logging in again — if the issue persists, use the Manual Session Entry option instead.

**API 429 errors in logs**
Grab is rate limiting requests. This is normal and the add-on will retry automatically. The fallback order history request includes a 3-second delay to reduce rate limiting.

**Enabling debug logs**
Set `log_level: debug` in the add-on configuration and restart. Debug logs include full API response structure, per-sensor push confirmations, and detailed cookie capture information — useful for reporting issues.