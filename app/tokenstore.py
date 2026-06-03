"""Persistent token storage — saves all Grab session cookies needed for API calls."""
import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

_LOGGER = logging.getLogger("grab.tokenstore")


class TokenStore:
    def __init__(self, path: str = "/data/grab_token.json") -> None:
        self._path = path
        data_dir = os.path.dirname(path) or "."
        self._order_path = os.path.join(data_dir, "grab_order.json")
        self._reauth_path = os.path.join(data_dir, "grab_reauth.json")
        self._data: dict = {}
        self._updated_at: str = ""
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Internal sync helpers — called via asyncio.to_thread
    # ------------------------------------------------------------------

    def _read_from_disk(self) -> dict:
        """Sync: read and return the saved JSON dict from disk."""
        with open(self._path) as f:
            return json.load(f)

    def _write_to_disk(self, payload: dict) -> None:
        """Sync: write payload dict to disk atomically via a temp file."""
        tmp = self._path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, self._path)

    def session_data_sync(self) -> dict:
        """Sync accessor used by GrabPoller via asyncio.to_thread.
        Returns the in-memory session data dict if valid, otherwise {}.
        Falls back to disk only if in-memory data is absent — this should not
        normally happen because load() completes before GrabPoller.start() is
        called. If it does fire, the warning below makes it visible.
        """
        if self._data.get("passenger_authn_token") and self._data.get("gfc_session"):
            return dict(self._data)
        # Unexpected path: in-memory data absent, falling back to disk.
        _LOGGER.warning(
            "session_data_sync: in-memory session absent — falling back to disk. "
            "This should only happen before load() completes."
        )
        try:
            with open(self._path) as f:
                saved = json.load(f)
            data = saved.get("data", {})
            if data.get("passenger_authn_token") and data.get("gfc_session"):
                return data
            return {}
        except FileNotFoundError:
            return {}
        except Exception as e:
            _LOGGER.warning("Could not read session file: %s", e)
            return {}

    # ------------------------------------------------------------------
    # Async public API
    # ------------------------------------------------------------------

    async def load(self) -> None:
        try:
            saved = await asyncio.to_thread(self._read_from_disk)
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
            payload = {"data": self._data, "updated_at": self._updated_at}
            try:
                await asyncio.to_thread(self._write_to_disk, payload)
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
        """Returns passenger_authn_token for display purposes."""
        return self._data.get("passenger_authn_token", "")

    @property
    def has_token(self) -> bool:
        return bool(self._data.get("passenger_authn_token"))

    @property
    def updated_at(self) -> str:
        return self._updated_at

    # ------------------------------------------------------------------
    # Order data persistence — last known order survives restarts
    # ------------------------------------------------------------------

    def save_order_sync(self, data: list[dict]) -> None:
        """Sync: write last orders list to disk. Called via asyncio.to_thread."""
        try:
            tmp = self._order_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, default=str)
            os.replace(tmp, self._order_path)
        except Exception as e:
            _LOGGER.warning("Failed to save order data: %s", e)

    def load_order_sync(self) -> Optional[list[dict] | dict]:
        """Sync: read last orders from disk. Returns list (new format) or dict (old format, handled in start())."""
        try:
            with open(self._order_path) as f:
                return json.load(f)
        except FileNotFoundError:
            return None
        except Exception as e:
            _LOGGER.warning("Failed to load order data: %s", e)
            return None

    # ------------------------------------------------------------------
    # Silent-reauth restart backoff — persisted so the loop breaker
    # survives the supervisor restarts that the reauth loop triggers.
    # ------------------------------------------------------------------

    def load_reauth_state_sync(self) -> dict:
        """Sync: read persisted reauth-restart backoff state. Returns {} if absent."""
        try:
            with open(self._reauth_path) as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except Exception as e:
            _LOGGER.warning("Failed to load reauth state: %s", e)
            return {}

    def save_reauth_state_sync(self, state: dict) -> None:
        """Sync: write reauth-restart backoff state to disk atomically."""
        try:
            tmp = self._reauth_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f)
            os.replace(tmp, self._reauth_path)
        except Exception as e:
            _LOGGER.warning("Failed to save reauth state: %s", e)