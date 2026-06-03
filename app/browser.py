"""Browser session manager — launches headed Chromium on Xvfb, captures Grab session cookies.

All browser-session state (lock, status, current task, on-demand display processes) is
owned by a single ``LoginManager`` instance. Both entry points — the UI login (main.py)
and the poller's silent reauth — go through it, so a UI login can cooperatively abort an
in-progress silent reauth instead of waiting out the reauth timeout.
"""
import asyncio
import base64
import json
import logging
import os
import shutil
import subprocess
import signal
from typing import Awaitable, Callable, Optional

_LOGGER = logging.getLogger("grab.browser")

GRAB_LOGIN_URL = "https://food.grab.com/auth/login"
PROFILE_DIR = "/data/browser_profile"
LOGIN_TIMEOUT = 180      # seconds user has to log in (normal flow)
REAUTH_TIMEOUT = 60      # seconds for silent re-authentication attempt

# Shared user-agent — keep in sync with poller.py
CHROME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/134.0.0.0 Safari/537.36"
)

# Chromium cache subdirectories that are safe to delete after each login.
# These are regenerated on next launch — deleting them keeps the profile lean.
_CACHE_DIRS = [
    "Cache",
    "Code Cache",
    "GPUCache",
    "Service Worker",
    "IndexedDB",
    "blob_storage",
    "Network",
]


def _decode_jwt_payload(value: str) -> dict:
    """Decode the payload of a JWT-like token. Returns {} if it can't be parsed."""
    parts = value.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)  # restore base64 padding
    return json.loads(base64.urlsafe_b64decode(payload).decode("utf-8", errors="replace"))


def extract_session_key(gfc_session_value: str) -> str:
    """Extract sessionKey from gfc_session JWT payload."""
    try:
        return _decode_jwt_payload(gfc_session_value).get("sessionKey", "")
    except Exception as e:
        _LOGGER.warning("Could not extract sessionKey from gfc_session: %s", e)
        return ""


def extract_country(gfc_session_value: str) -> str:
    """Extract the country code from the gfc_session JWT payload (uppercased), or "" if absent.

    NOTE: the browser flow reads country from the separate ``gfc_country`` cookie; the claim
    name inside the JWT is unconfirmed, so several candidates are tried and an empty string is
    returned (caller falls back to a default) if none are present.
    """
    try:
        payload = _decode_jwt_payload(gfc_session_value)
        for key in ("countryCode", "country", "cc", "countryISO"):
            val = payload.get(key)
            if val:
                return str(val).upper()
    except Exception as e:
        _LOGGER.debug("Could not extract country from gfc_session: %s", e)
    return ""


class LoginManager:
    """Owns the headed-Chromium login/reauth lifecycle and all of its mutable state."""

    def __init__(self) -> None:
        self._state = {"status": "idle", "running": False, "error": ""}
        # Serialises the check-and-set of the running flag + task handover. Held only briefly;
        # the long-running browser session runs outside it so a competing login can preempt.
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._abort = asyncio.Event()  # cooperative abort signal, checked by the capture loop
        self._xvfb_proc: Optional[subprocess.Popen] = None
        self._x11vnc_proc: Optional[subprocess.Popen] = None

    def get_state(self) -> dict:
        return dict(self._state)

    # ------------------------------------------------------------------
    # Public API — the two entry points + cancel
    # ------------------------------------------------------------------

    async def start_login(
        self,
        on_token: Callable[[dict], Awaitable[None]],
        on_success: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> bool:
        """UI-triggered login. Preempts an in-progress silent reauth; declines if a UI
        login is already running. Returns True if a login task was started."""
        async with self._lock:
            if self._state["running"]:
                if self._state["status"] == "reauth":
                    await self._abort_current_locked()  # cooperatively cancel the reauth
                else:
                    _LOGGER.warning("Login already in progress.")
                    return False
            self._state.update(running=True, status="launching", error="")
            self._abort.clear()
            self._task = asyncio.create_task(
                self._run(on_token=on_token, silent=False, on_success=on_success)
            )
        return True

    async def request_reauth(
        self,
        on_token: Callable[[dict], Awaitable[None]],
        on_success: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> bool:
        """Poller-triggered silent reauth against the saved profile. Declines if anything is
        already running. Awaits the result; returns False if a competing login aborts it."""
        async with self._lock:
            if self._state["running"]:
                return False
            self._state.update(running=True, status="reauth", error="")
            self._abort.clear()
            task = asyncio.create_task(
                self._run(on_token=on_token, silent=True, on_success=on_success)
            )
            self._task = task
        # Await outside the lock so a UI login can preempt mid-flight.
        try:
            return await task
        except asyncio.CancelledError:
            # Our awaiter (the poll loop) is being torn down, e.g. on shutdown.
            # Signal the reauth to wind down cooperatively; don't block teardown.
            self._abort.set()
            raise

    async def cancel(self) -> None:
        """Abort whatever is running (UI cancel button / endpoint)."""
        async with self._lock:
            await self._abort_current_locked()

    async def _abort_current_locked(self) -> None:
        """Signal the current task to abort and wait for it to clean up. Caller holds the lock."""
        task = self._task
        if task and not task.done():
            self._abort.set()
            try:
                await task
            except Exception:
                pass

    # ------------------------------------------------------------------
    # On-demand virtual display
    # ------------------------------------------------------------------

    async def _start_display(self) -> None:
        """Start Xvfb and x11vnc on demand. No-op if already running."""
        if self._xvfb_proc is None or self._xvfb_proc.poll() is not None:
            _LOGGER.info("Starting Xvfb on :99...")
            self._xvfb_proc = subprocess.Popen(
                ["Xvfb", ":99", "-screen", "0", "1280x800x24", "-ac"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            await asyncio.sleep(1)
            _LOGGER.info("Xvfb started (PID %s).", self._xvfb_proc.pid)

        if self._x11vnc_proc is None or self._x11vnc_proc.poll() is not None:
            _LOGGER.info("Starting x11vnc...")
            self._x11vnc_proc = subprocess.Popen(
                [
                    "x11vnc", "-display", ":99",
                    "-nopw", "-listen", "127.0.0.1",
                    "-rfbport", "5900",
                    "-forever", "-shared", "-quiet",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            await asyncio.sleep(1)
            _LOGGER.info("x11vnc started (PID %s).", self._x11vnc_proc.pid)

    async def _stop_display(self) -> None:
        """Stop Xvfb and x11vnc if running."""
        for name, proc in [("x11vnc", self._x11vnc_proc), ("Xvfb", self._xvfb_proc)]:
            if proc is not None and proc.poll() is None:
                try:
                    proc.send_signal(signal.SIGTERM)
                    await asyncio.to_thread(proc.wait, 5)
                    _LOGGER.info("%s stopped.", name)
                except Exception as e:
                    _LOGGER.debug("Could not stop %s cleanly: %s", name, e)
                    try:
                        proc.kill()
                    except Exception:
                        pass
        self._xvfb_proc = None
        self._x11vnc_proc = None

    # ------------------------------------------------------------------
    # Profile housekeeping
    # ------------------------------------------------------------------

    @staticmethod
    def _clear_profile_locks(profile_dir: str) -> None:
        """Remove Chromium singleton lock files that can prevent profile loading.
        These are left behind if a previous session crashed or was killed uncleanly.
        """
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            path = os.path.join(profile_dir, name)
            try:
                os.remove(path)
                _LOGGER.debug("Removed stale lock file: %s", path)
            except FileNotFoundError:
                pass
            except Exception as e:
                _LOGGER.debug("Could not remove lock file %s: %s", path, e)

    @staticmethod
    def _cleanup_browser_cache(profile_dir: str) -> None:
        """Delete expendable Chromium cache subdirectories from the browser profile.
        Cookies and session data are preserved — only regenerable cache is removed.
        """
        for name in _CACHE_DIRS:
            path = os.path.join(profile_dir, "Default", name)
            if os.path.exists(path):
                try:
                    shutil.rmtree(path)
                    _LOGGER.debug("Cleared browser cache dir: %s", path)
                except Exception as e:
                    _LOGGER.debug("Could not clear browser cache dir %s: %s", path, e)

    # ------------------------------------------------------------------
    # The browser session itself
    # ------------------------------------------------------------------

    async def _run(
        self,
        on_token: Callable[[dict], Awaitable[None]],
        silent: bool,
        on_success: Optional[Callable[[], Awaitable[None]]],
    ) -> bool:
        """Launch headed Chromium, navigate to Grab login, capture session cookies.

        State (running flag / status) is set by the caller before this task is created.
        Returns True if a session was captured, False on timeout / abort / error.
        """
        timeout = REAUTH_TIMEOUT if silent else LOGIN_TIMEOUT
        success = False

        try:
            from playwright.async_api import async_playwright

            if silent:
                _LOGGER.info("Attempting silent re-authentication via saved browser profile...")
            else:
                _LOGGER.info("Launching headed Chromium on display :99...")

            await self._start_display()

            async with async_playwright() as p:
                launch_env = {**os.environ, "DISPLAY": ":99"}

                os.makedirs(PROFILE_DIR, exist_ok=True)
                await asyncio.to_thread(self._clear_profile_locks, PROFILE_DIR)
                _LOGGER.info("Launching Chromium persistent context (silent=%s)...", silent)
                try:
                    context = await asyncio.wait_for(
                        p.chromium.launch_persistent_context(
                            PROFILE_DIR,
                            headless=False,
                            args=[
                                "--no-sandbox",
                                "--disable-setuid-sandbox",
                                "--disable-dev-shm-usage",
                                "--disable-gpu",
                                "--window-size=1280,800",
                                "--window-position=0,0",
                                "--disable-blink-features=AutomationControlled",
                            ],
                            ignore_default_args=["--enable-automation"],
                            env=launch_env,
                            viewport={"width": 1280, "height": 800},
                            user_agent=CHROME_USER_AGENT,
                        ),
                        timeout=30.0,
                    )
                except asyncio.TimeoutError:
                    _LOGGER.error("Chromium launch timed out after 30s — aborting.")
                    self._state["status"] = "error"
                    self._state["error"] = "Chromium launch timeout"
                    context = None

                if context is not None:
                    _LOGGER.info("Chromium context ready.")

                    await context.add_init_script(
                        "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
                    )

                    page = await context.new_page()

                    def _on_page_error(err) -> None:
                        _LOGGER.warning("Grab login page JS error: %s", err)

                    page.on("pageerror", _on_page_error)

                    if silent:
                        # Silent reauth — navigate to the login URL so Chromium establishes the
                        # origin context. Without a prior navigation, context.cookies(url) returns
                        # empty even when cookies exist in the profile.
                        _LOGGER.info("Silent reauth — navigating to establish origin context...")
                    else:
                        _LOGGER.info("Navigating to Grab login...")
                        self._state["status"] = "waiting_login"

                    await page.goto(GRAB_LOGIN_URL, wait_until="domcontentloaded", timeout=30000)

                    session_data = await self._capture_cookies_loop(context, timeout, silent)

                    await context.close()

                    # Clean up Chromium cache dirs from the profile to prevent unbounded growth.
                    await asyncio.to_thread(self._cleanup_browser_cache, PROFILE_DIR)

                    if session_data and not self._abort.is_set():
                        _LOGGER.info(
                            "%s captured successfully.",
                            "Silent re-authentication" if silent else "Session",
                        )
                        self._state["status"] = "captured"
                        await on_token(session_data)
                        if on_success:
                            await on_success()
                        success = True
                    elif self._abort.is_set():
                        _LOGGER.info("Browser session aborted by request.")
                        self._state["status"] = "idle"
                    else:
                        if silent:
                            _LOGGER.debug(
                                "Silent re-authentication found no valid cookies in profile after %ds.",
                                timeout,
                            )
                        else:
                            _LOGGER.error("Login timed out after %ds — user action required.", timeout)
                        self._state["status"] = "timeout"

        except Exception as exc:
            _LOGGER.error("Browser error: %s", exc)
            self._state["status"] = "error"
            self._state["error"] = str(exc)
        finally:
            await self._stop_display()
            self._state["running"] = False

        return success

    async def _capture_cookies_loop(self, context, timeout: int, silent: bool) -> Optional[dict]:
        """Poll the profile cookies until the required session cookies appear, the timeout
        elapses, or an abort is requested. Returns the session dict or None."""
        elapsed = 0
        poll = 2

        while elapsed < timeout and not self._abort.is_set():
            cookies = await context.cookies("https://food.grab.com")
            cookie_map = {c["name"]: c["value"] for c in cookies if c.get("value")}

            authn = cookie_map.get("passenger_authn_token", "")
            gfc = cookie_map.get("gfc_session", "")
            gfc_guid = cookie_map.get("gfc_session_guid", "")
            gfc_country = cookie_map.get("gfc_country", "MY").upper()

            if authn and gfc:
                session_key = extract_session_key(gfc)
                if not session_key:
                    _LOGGER.warning(
                        "session_key extraction returned empty — API calls may fail. "
                        "gfc_session prefix: %s...", gfc[:20]
                    )
                else:
                    _LOGGER.debug(
                        "Captured cookies (country=%s); session_key present.", gfc_country
                    )
                if not gfc_guid:
                    _LOGGER.debug(
                        "gfc_session_guid not present — may be optional for region %s", gfc_country
                    )

                return {
                    "passenger_authn_token": authn,
                    "gfc_session": gfc,
                    "gfc_session_guid": gfc_guid,
                    "session_key": session_key,
                    "country": gfc_country,
                }

            if not silent and elapsed > 0 and elapsed % 10 == 0:
                _LOGGER.info("Waiting for login... cookies so far: %s", list(cookie_map.keys()))

            await asyncio.sleep(poll)
            elapsed += poll

        return None
