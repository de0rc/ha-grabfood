"""JWT / cookie helper tests — no Playwright or network required."""
import base64
import json

import browser


def _make_jwt(payload: dict) -> str:
    """Build a JWT-shaped string with an unpadded base64url payload (as real tokens are)."""
    raw = json.dumps(payload).encode("utf-8")
    b64 = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"header.{b64}.signature"


def test_decode_payload_roundtrips_across_padding_lengths():
    # Vary the JSON length so the base64 length lands on every len % 4 residue,
    # exercising the `-len % 4` padding fix (the old `4 - len % 4` over-padded at residue 0).
    for n in range(0, 8):
        payload = {"sessionKey": "k" * n, "countryCode": "SG"}
        decoded = browser._decode_jwt_payload(_make_jwt(payload))
        assert decoded == payload


def test_extract_session_key():
    assert browser.extract_session_key(_make_jwt({"sessionKey": "abc123"})) == "abc123"


def test_extract_session_key_missing_returns_empty():
    assert browser.extract_session_key(_make_jwt({"foo": "bar"})) == ""
    assert browser.extract_session_key("not-a-jwt") == ""


def test_extract_country_variants_and_uppercase():
    assert browser.extract_country(_make_jwt({"countryCode": "sg"})) == "SG"
    assert browser.extract_country(_make_jwt({"country": "id"})) == "ID"
    assert browser.extract_country(_make_jwt({"sessionKey": "x"})) == ""
    assert browser.extract_country("garbage") == ""
