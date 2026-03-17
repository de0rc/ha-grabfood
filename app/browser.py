"""Browser session manager — launches headed Chromium on Xvfb, captures Grab session cookies."""
import asyncio
import base64
import json
import logging
import os
import shutil
import subprocess
import signal
from typing import Callable, Optional

_LOGGER = logging.getLogger("grab.browser")

GRAB_LOGIN_URL = "https://food.grab.com/auth/login"
PROFILE_DIR = "/data/browser_profile"
LOGIN_TIMEOUT = 180      # seconds user has to log in (normal flow)
REAUTH_TIMEOUT = 60      # seconds for silent re-authentication attempt

# Shared user-agent — keep in sync with poller.py
CHROME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

_state = {
    "status": "idle",
    "running": False,
    "error": "",
}

# Holds references to on-demand display processes
_xvfb_proc: Optional[subprocess.Popen] = None
_x11vnc_proc: Optional[subprocess.Popen] = None


async def _start_display() -> None:
    """Start Xvfb and x11vnc on demand. No-op if already running."""
    global _xvfb_proc, _x11vnc_proc

    if _xvfb_proc is None or _xvfb_proc.poll() is not None:
        _LOGGER.info("Starting Xvfb on :99...")
        _xvfb_proc = subprocess.Popen(
            ["Xvfb", ":99", "-screen", "0", "1280x800x24", "-ac"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        await asyncio.sleep(1)
        _LOGGER.info("Xvfb started (PID %s).", _xvfb_proc.pid)

    if _x11vnc_proc is None or _x11vnc_proc.poll() is not None:
        _LOGGER.info("Starting x11vnc...")
        _x11vnc_proc = subprocess.Popen(
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
        _LOGGER.info("x11vnc started (PID %s).", _x11vnc_proc.pid)


async def _stop_display() -> None:
    """Stop Xvfb and x11vnc if running."""
    global _xvfb_proc, _x11vnc_proc

    for name, proc in [("x11vnc", _x11vnc_proc), ("Xvfb", _xvfb_proc)]:
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

    _xvfb_proc = None
    _x11vnc_proc = None


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


def _clear_profile_locks(profile_dir: str) -> None:
    """Remove Chromium singleton lock files that can prevent profile loading.
    These are left behind if a previous session crashed or was killed uncleanly.
    """
    lock_files = [
        os.path.join(profile_dir, "SingletonLock"),
        os.path.join(profile_dir, "SingletonCookie"),
        os.path.join(profile_dir, "SingletonSocket"),
    ]
    for path in lock_files:
        try:
            os.remove(path)
            _LOGGER.debug("Removed stale lock file: %s", path)
        except FileNotFoundError:
            pass
        except Exception as e:
            _LOGGER.debug("Could not remove lock file %s: %s", path, e)


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


def get_state() -> dict:
    return dict(_state)


def _extract_session_key(gfc_session_value: str) -> str:
    """Extract sessionKey from gfc_session JWT payload."""
    try:
        parts = gfc_session_value.split(".")
        if len(parts) < 2:
            return ""
        payload = parts[1]
        payload += "=" * (4 - len(payload) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8", errors="replace"))
        return decoded.get("sessionKey", "")
    except Exception as e:
        _LOGGER.warning("Could not extract sessionKey from gfc_session: %s", e)
        return ""


async def launch_login(
    on_token: Callable[[dict], None],
    silent: bool = False,
    on_success: Optional[Callable[[], None]] = None,
) -> bool:
    """Launch headed Chromium, navigate to Grab login, capture all required session cookies.

    Args:
        on_token:   Callback invoked with session data dict on successful capture.
        silent:     If True, uses REAUTH_TIMEOUT and sets status to 'reauth'.
                    Used for automatic re-authentication against saved browser profile.
                    If False (default), uses LOGIN_TIMEOUT and normal UI flow.
        on_success: Optional async callback invoked after on_token completes.
                    Used to trigger a supervisor restart to reclaim memory.

    Returns:
        True if session was captured successfully, False otherwise.
    """
    if _state["running"]:
        _LOGGER.warning("Login already in progress.")
        return False

    _state["running"] = True
    _state["status"] = "reauth" if silent else "launching"
    _state["error"] = ""

    timeout = REAUTH_TIMEOUT if silent else LOGIN_TIMEOUT
    success = False

    try:
        from playwright.async_api import async_playwright

        if silent:
            _LOGGER.info("Attempting silent re-authentication via saved browser profile...")
        else:
            _LOGGER.info("Launching headed Chromium on display :99...")

        await _start_display()

        async with async_playwright() as p:
            launch_env = {**os.environ, "DISPLAY": ":99"}

            os.makedirs(PROFILE_DIR, exist_ok=True)
            await asyncio.to_thread(_clear_profile_locks, PROFILE_DIR)
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
                _state["status"] = "error"
                _state["error"] = "Chromium launch timeout"
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
                    # empty even when cookies exist in the profile (root cause of the 60s timeout:
                    # confirmed that manual login captures cookies immediately after goto() because
                    # the profile is already authenticated and no user input is needed).
                    _LOGGER.info("Silent reauth — navigating to establish origin context...")
                else:
                    _LOGGER.info("Navigating to Grab login...")
                    _state["status"] = "waiting_login"

                await page.goto(GRAB_LOGIN_URL, wait_until="domcontentloaded", timeout=30000)

                session_data = None
                elapsed = 0
                poll = 2

                while elapsed < timeout:
                    cookies = await context.cookies("https://food.grab.com")
                    cookie_map = {c["name"]: c["value"] for c in cookies if c.get("value")}

                    authn = cookie_map.get("passenger_authn_token", "")
                    gfc = cookie_map.get("gfc_session", "")
                    gfc_guid = cookie_map.get("gfc_session_guid", "")
                    gfc_country = cookie_map.get("gfc_country", "MY").upper()

                    if authn and gfc:
                        session_key = _extract_session_key(gfc)
                        if not session_key:
                            _LOGGER.warning(
                                "session_key extraction returned empty — API calls may fail. "
                                "gfc_session prefix: %s...", gfc[:20]
                            )
                        else:
                            log = _LOGGER.debug if silent else _LOGGER.info
                            log(
                                "Captured: passenger_authn_token=%s... gfc_session=%s... "
                                "session_key=%s... country=%s",
                                authn[:20], gfc[:20], session_key[:20], gfc_country
                            )
                        if not gfc_guid:
                            _LOGGER.debug(
                                "gfc_session_guid not present — may be optional for region %s",
                                gfc_country
                            )
                        else:
                            _LOGGER.debug("gfc_session_guid captured: %s...", gfc_guid[:20])

                        session_data = {
                            "passenger_authn_token": authn,
                            "gfc_session": gfc,
                            "gfc_session_guid": gfc_guid,
                            "session_key": session_key,
                            "country": gfc_country,
                        }
                        break

                    if not silent and elapsed > 0 and elapsed % 10 == 0:
                        _LOGGER.info(
                            "Waiting for login... cookies so far: %s", list(cookie_map.keys())
                        )

                    await asyncio.sleep(poll)
                    elapsed += poll

                await context.close()

                # Clean up Chromium cache dirs from the profile to prevent unbounded growth.
                # Cookies are kept (they're what we need); only expendable cache is removed.
                await asyncio.to_thread(_cleanup_browser_cache, PROFILE_DIR)

                if session_data:
                    _LOGGER.info(
                        "%s captured successfully.",
                        "Silent re-authentication" if silent else "Session"
                    )
                    _state["status"] = "captured"
                    await on_token(session_data)
                    if on_success:
                        await on_success()
                    success = True
                else:
                    if silent:
                        _LOGGER.debug(
                            "Silent re-authentication found no valid cookies in profile after %ds.",
                            timeout
                        )
                    else:
                        _LOGGER.error(
                            "Login timed out after %ds — user action required.", timeout
                        )
                    _state["status"] = "timeout"

    except Exception as exc:
        _LOGGER.error("Browser error: %s", exc)
        _state["status"] = "error"
        _state["error"] = str(exc)
    finally:
        await _stop_display()
        _state["running"] = False

    return success


async def try_silent_reauth(
    on_token: Callable[[dict], None],
    on_success: Optional[Callable[[], None]] = None,
) -> bool:
    """Attempt silent re-authentication using the saved browser profile.
    Returns True if new session cookies were captured, False otherwise.
    """
    return await launch_login(on_token=on_token, silent=True, on_success=on_success)