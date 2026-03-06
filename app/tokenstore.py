"""Persistent token storage — saves all Grab session cookies needed for API calls."""
import asyncio
import json
import logging
import os
from datetime import datetime, timezone

_LOGGER = logging.getLogger("grab.tokenstore")


class TokenStore:
    def __init__(self, path: str = "/data/grab_token.json") -> None:
        self._path = path
        self._data: dict = {}
        self._updated_at: str = ""
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        try:
            with open(self._path) as f:
                saved = json.load(f)
                self._data = saved.get("data", {})
                self._updated_at = saved.get("updated_at", "")
                if self._data:
                    _LOGGER.info("Loaded existing session (updated %s)", self._updated_at)
        except FileNotFoundError:
            _LOGGER.info("No existing session — login required.")
        except Exception as exc:
            _LOGGER.warning("Failed to load session: %s", exc)

    async def save(self, data: dict) -> None:
        """Save session data dict containing passenger_authn_token, gfc_session, session_key."""
        async with self._lock:
            self._data = data
            self._updated_at = datetime.now(timezone.utc).isoformat()
            try:
                with open(self._path, "w") as f:
                    json.dump({"data": self._data, "updated_at": self._updated_at}, f)
                _LOGGER.info("Session saved at %s", self._updated_at)
            except Exception as exc:
                _LOGGER.error("Failed to save session: %s", exc)

    async def clear(self) -> None:
        async with self._lock:
            self._data = {}
            self._updated_at = ""
            try:
                os.remove(self._path)
            except FileNotFoundError:
                pass

    @property
    def token(self) -> str:
        """Legacy compat — returns passenger_authn_token."""
        return self._data.get("passenger_authn_token", "")

    @property
    def has_token(self) -> bool:
        return bool(self._data.get("passenger_authn_token"))

    @property
    def updated_at(self) -> str:
        return self._updated_at

    @property
    def session_data(self) -> dict:
        return dict(self._data)