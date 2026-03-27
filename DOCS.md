# GrabFood Tracker

Tracks your GrabFood delivery orders in Home Assistant.

## Sensors

| Entity | State | Attributes |
|--------|-------|------------|
| `sensor.grabfood_orders` | active order count (integer) | `orders` — list of order objects; `active_count` |

Each object in the `orders` attribute list contains:

| Field | Description |
|-------|-------------|
| `order_id` | Order identifier |
| `order_status` | Raw API status string (e.g. `PICKING_UP`, `FOOD_COLLECTED`, `COMPLETED`) |
| `restaurant` | Restaurant name |
| `eta` | ISO 8601 delivery timestamp |
| `eta_minutes` | Minutes until delivery (integer) |
| `active_order` | `true` while the order is in progress |

## Map card

The add-on ships a custom Lovelace card (`grabfood-map-card`) that displays your home location, all active driver positions, and a route line from each driver to your home. It is automatically installed and registered as a Lovelace resource on startup — no manual setup required.

**Add the card to any dashboard:**

```yaml
type: custom:grabfood-map-card
entity: sensor.grabfood_orders
```

The card also appears in the Lovelace card picker — click **Add card** and search for "GrabFood".

**Optional config:**

```yaml
type: custom:grabfood-map-card
entity: sensor.grabfood_orders
show_info: true        # show restaurant name + ETA below the map (default: true)
height: 400            # map height in pixels (default: 400)
aspect_ratio: "16/9"   # overrides height when set
```

A visual editor is available — click the pencil icon on the card to configure it without YAML.

The card uses CartoDB Voyager tiles with automatic dark mode support, and [OSRM](https://project-osrm.org/) for routing — no API keys required.

## Force poll

POST to `/poll/force` via the add-on ingress to wake the poll loop immediately:

```yaml
rest_command:
  grabfood_force_poll:
    url: "http://homeassistant:8099/poll/force"
    method: POST
```

## HA automation — order state changes

The add-on fires a `grabfood_order_state_changed` event whenever an order's status transitions:

```yaml
automation:
  - alias: "Notify when food is collected"
    trigger:
      - platform: event
        event_type: grabfood_order_state_changed
        event_data:
          order_status: FOOD_COLLECTED
    action:
      - service: notify.mobile_app
        data:
          message: "Your order from {{ trigger.event.data.restaurant }} is on the way!"
```

## Dashboard examples

### Show order details with Mushroom chips

Requires the [Mushroom](https://github.com/piitaya/lovelace-mushroom) custom card.

```yaml
type: custom:mushroom-chips-card
chips:
  - type: template
    icon: mdi:store
    content: >
      {{ state_attr('sensor.grabfood_orders', 'orders')[0].restaurant
         if state_attr('sensor.grabfood_orders', 'orders') else '' }}
  - type: template
    icon: mdi:clock
    content: >
      {% set orders = state_attr('sensor.grabfood_orders', 'orders') %}
      {% if orders %}
        {{ orders[0].eta | as_timestamp | timestamp_custom('%-I:%M %p') }}
        ({{ orders[0].eta_minutes }} min)
      {% endif %}
```

> **Note:** These templates show details for the first active order. For multiple simultaneous orders, use the map card — each driver pin shows restaurant, ETA, and status.

### Hide cards when no order is active

```yaml
type: conditional
conditions:
  - condition: numeric_state
    entity: sensor.grabfood_orders
    above: 0
card:
  type: custom:grabfood-map-card
  entity: sensor.grabfood_orders
```

## Poll intervals

| State | Interval |
|-------|----------|
| `FOOD_COLLECTED`, `DRIVER_ARRIVED` | 30 s |
| `ALLOCATING`, `PICKING_UP`, `DRIVER_AT_STORE` | 60 s |
| `COMPLETED`, `CANCELLED`, idle | 300 s |

## Memory management

After every successful login or silent re-authentication, the add-on requests a supervisor restart to reclaim the memory held by Playwright and Chromium. The session is saved to disk before the restart fires — nothing is lost.
