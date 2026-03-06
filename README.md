# GrabFood Tracker

A Home Assistant add-on that tracks your GrabFood delivery orders and exposes them as sensors in Home Assistant.

## Features

- Tracks live GrabFood order status
- Exposes order data as HA sensors (status, ETA, restaurant, driver location)
- Optional driver map tracker (`device_tracker.grabfood_driver`)
- Browser-based login via noVNC — no manual cookie extraction needed
- Automatic session persistence across restarts

## Sensors Created

| Entity | Description |
|--------|-------------|
| `sensor.grabfood_order_status` | Current order state (e.g. Allocating, Food Collected, Completed) |
| `sensor.grabfood_eta` | Estimated delivery time (ISO timestamp) |
| `sensor.grabfood_eta_minutes` | ETA in minutes |
| `sensor.grabfood_restaurant` | Restaurant name |
| `sensor.grabfood_active_order` | Whether an order is currently active (`on`/`off`) |
| `device_tracker.grabfood_driver` | Driver location on map (optional) |

## Installation

1. Add this repository to your Home Assistant add-on store:
   - Go to **Settings → Add-ons → Add-on Store**
   - Click the three-dot menu → **Repositories**
   - Add: `https://github.com/de0rc/ha-grabfood`

2. Install the **GrabFood Tracker** add-on.

3. Start the add-on and open the Web UI from the sidebar.

4. Click **Start Login** — a browser window will open via noVNC.

5. Log in to your GrabFood account. The session will be captured automatically.

## Configuration

| Option | Default | Description |
|--------|---------|-------------|
| `show_driver_map` | `false` | Enable driver location tracking on HA map |

## How It Works

The add-on launches a headless Chromium browser (via Playwright) on a virtual display (Xvfb), navigates to the GrabFood login page, and captures session cookies once you log in. These cookies are then used to poll the GrabFood API every 30–300 seconds depending on order state.

## Poll Intervals

| State | Interval |
|-------|----------|
| `FOOD_COLLECTED`, `DRIVER_ARRIVED` | 30 seconds |
| `ALLOCATING`, `PICKING_UP`, `DRIVER_AT_STORE` | 60 seconds |
| `COMPLETED`, `CANCELLED`, `FAILED` | 5 minutes |

## Requirements

- Home Assistant OS or Supervised
- amd64 or aarch64 architecture