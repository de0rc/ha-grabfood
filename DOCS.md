# GrabFood Tracker

## First Time Setup

1. Start the add-on and open the Web UI from the sidebar.
2. Click **Start Login** — a browser window will open via noVNC.
3. Log in to your GrabFood account normally.
4. The session will be captured automatically — the browser will close once done.
5. Your sensors will start updating within 60 seconds.

## Configuration

### `show_driver_map` (default: `false`)

Enable this to track your delivery driver's location on the Home Assistant map.

When enabled, a `device_tracker.grabfood_driver` entity will be created and updated with the driver's GPS coordinates in real time. You can add this to a Map card in your dashboard.

## Sensors

| Entity | Description |
|--------|-------------|
| `sensor.grabfood_order_status` | Current order state (e.g. Allocating, Food Collected, Completed) |
| `sensor.grabfood_eta` | Estimated delivery time (ISO timestamp) |
| `sensor.grabfood_eta_minutes` | Minutes until delivery |
| `sensor.grabfood_restaurant` | Restaurant name |
| `sensor.grabfood_active_order` | `on` when an order is active, `off` otherwise |

## Session Expiry

If your GrabFood session expires, you will receive a notification in Home Assistant asking you to log in again. Simply open the Web UI from the sidebar and click **Start Login**.

## Supported Countries

This add-on works with any country where GrabFood is available. The country is detected automatically from your GrabFood account when you log in.

## Troubleshooting

**Sensors not updating** — check that you are logged in via the Web UI. If the session has expired, log in again.

**noVNC screen is black** — wait a few seconds and refresh. Xvfb may still be starting up.

**API 429 errors in logs** — Grab is rate limiting requests. This is normal and the add-on will retry automatically.
