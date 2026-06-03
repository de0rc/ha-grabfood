"""_extract_order_data parsing tests."""
import poller


def _order(**overrides) -> dict:
    base = {
        "orderID": "ORD-1",
        "orderState": "PICKING_UP",
        "snapshotDetail": {
            "cartWithQuote": {
                "merchantCartWithQuoteList": [
                    {"merchantInfoObj": {"name": "Nasi Lemak House"}}
                ]
            }
        },
        "driverTrack": {"location": {"latitude": 3.14, "longitude": 101.6}, "minETAInMin": 12},
        "orderMeta": {"expectedTime": "2026-06-03T10:00:00Z"},
    }
    base.update(overrides)
    return base


def test_full_order():
    out = poller._extract_order_data(_order())
    assert out["order_id"] == "ORD-1"
    assert out["order_status"] == "PICKING_UP"
    assert out["restaurant"] == "Nasi Lemak House"
    assert out["driver_lat"] == 3.14
    assert out["driver_lon"] == 101.6
    assert out["eta_minutes"] == 12
    assert out["active_order"] is True


def test_missing_order_state_becomes_unknown_and_inactive():
    out = poller._extract_order_data(_order(orderState=None))
    assert out["order_status"] == "UNKNOWN"
    assert out["active_order"] is False


def test_null_driver_track_does_not_crash():
    out = poller._extract_order_data(_order(driverTrack=None))
    assert out["driver_lat"] is None
    assert out["driver_lon"] is None


def test_equatorial_zero_latitude_is_preserved():
    # lat 0.0 is a real coordinate (e.g. Pontianak) and must not be dropped as falsy.
    out = poller._extract_order_data(_order(driverTrack={"location": {"latitude": 0.0, "longitude": 109.3}}))
    assert out["driver_lat"] == 0.0
    assert out["driver_lon"] == 109.3


def test_completed_order_is_inactive():
    out = poller._extract_order_data(_order(orderState="COMPLETED"))
    assert out["active_order"] is False
