# GrabFood Tracker

A Home Assistant add-on that tracks your GrabFood delivery orders and exposes them as sensors and a live map card.

## Features

- Tracks live GrabFood order status — supports multiple simultaneous orders
- Single `sensor.grabfood_orders` entity (state = active count, attributes hold full order list)
- Custom Lovelace map card with driver pins, route lines, and ETA header
- Browser-based login via noVNC — no manual cookie extraction needed
- Automatic re-authentication on session expiry using saved browser profile
- Persistent session and sensor state across add-on restarts

## Sensor

| Entity | State | Attributes |
|--------|-------|------------|
| `sensor.grabfood_orders` | Active order count (integer) | `orders` list, `active_count` |

Each item in `orders` contains: `order_id`, `order_status`, `restaurant`, `eta`, `eta_minutes`, `active_order`.

## Map Card

The add-on ships a custom Lovelace card that is automatically installed and registered on startup.

```yaml
type: custom:grabfood-map-card
entity: sensor.grabfood_orders
```

- Driver pins (one per active order, distinct colours)
- OSRM route lines from each driver to home
- Restaurant name + ETA header below the map
- Automatic dark mode, fit-to-all button, visual editor

## Installation

1. Add this repository to your Home Assistant add-on store:
   - Go to **Settings → Add-ons → Add-on Store**
   - Click the three-dot menu → **Repositories**
   - Add: `https://github.com/de0rc/ha-grabfood`

2. Install the **GrabFood Tracker** add-on.

3. Start the add-on and open the Web UI from the sidebar.

4. Click **Start Login** — a browser window will open via noVNC.

5. Log in to your GrabFood account. The session is captured automatically.

> **Alternative:** If the browser login doesn't work in your environment, use the **Manual session entry** panel to paste your `passenger_authn_token` and `gfc_session` cookies directly.

## Configuration

| Option | Default | Description |
|--------|---------|-------------|
| `log_level` | `info` | Log verbosity: `debug`, `info`, `warning`, `error` |

## Poll Intervals

| State | Interval |
|-------|----------|
| `FOOD_COLLECTED`, `DRIVER_ARRIVED` | 30 s |
| `ALLOCATING`, `PICKING_UP`, `DRIVER_AT_STORE` | 60 s |
| `COMPLETED`, `CANCELLED`, idle | 5 min |

Interval is driven by the most urgent active order when multiple orders are in flight.

## Requirements

- Home Assistant OS or Supervised
- amd64 or aarch64 architecture
