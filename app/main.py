"""GrabFood Tracker — web server entry point."""
import asyncio
import logging
import sys
import os

_override = "/config/grabfood_tracker"
if os.path.isdir(_override):
    sys.path.insert(0, _override)

from aiohttp import web, WSMsgType
from jinja2 import Environment, FileSystemLoader

from tokenstore import TokenStore
from browser import launch_login, get_state
from poller import GrabPoller
from bridge import Bridge

_log_level = os.environ.get("LOG_LEVEL", "info").upper()
logging.basicConfig(
    level=getattr(logging, _log_level, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
_LOGGER = logging.getLogger("grab.main")

token_store = TokenStore(path="/data/grab_token.json")

NOVNC_DIR = "/opt/novnc"
VNC_HOST = "127.0.0.1"
VNC_PORT = 5900

_JINJA_ENV = None
_login_lock = asyncio.Lock()


def jinja() -> Environment:
    global _JINJA_ENV
    if _JINJA_ENV is None:
        _JINJA_ENV = Environment(loader=FileSystemLoader("/app/templates"), autoescape=True)
    return _JINJA_ENV


async def handle_index(request: web.Request) -> web.Response:
    ingress_path = request.headers.get("X-Ingress-Path", "")
    raw_ts = token_store.updated_at
    if raw_ts and len(raw_ts) >= 19:
        token_updated_at_display = raw_ts[:19].replace("T", " ") + " UTC"
    else:
        token_updated_at_display = ""
    tmpl = jinja().get_template("index.html")
    html = tmpl.render(
        ingress_path=ingress_path,
        has_token=token_store.has_token,
        token_updated_at_display=token_updated_at_display,
        browser_state=get_state(),
    )
    return web.Response(text=html, content_type="text/html")


async def handle_login_start(request: web.Request) -> web.Response:
    async with _login_lock:
        if get_state()["running"]:
            return web.json_response({"ok": False, "error": "Login already in progress"})

        async def on_token(token: dict):
            await token_store.save(token)

        asyncio.create_task(launch_login(on_token=on_token))
    return web.json_response({"ok": True})


async def handle_login_status(request: web.Request) -> web.Response:
    return web.json_response({
        **get_state(),
        "has_token": token_store.has_token,
        "token_updated_at": token_store.updated_at,
    })


async def handle_token_value(request: web.Request) -> web.Response:
    return web.json_response({
        "has_token": token_store.has_token,
        "token": token_store.token[:20] + "..." if token_store.has_token else "",
        "updated_at": token_store.updated_at,
    })


async def handle_manual_token(request: web.Request) -> web.Response:
    try:
        body = await request.json()
        authn = (body.get("passenger_authn_token") or "").strip()
        gfc = (body.get("gfc_session") or "").strip()
        if not authn or not gfc:
            return web.json_response({"ok": False, "error": "Both passenger_authn_token and gfc_session are required"}, status=400)
        from browser import _extract_session_key
        session_key = _extract_session_key(gfc)
        await token_store.save({
            "passenger_authn_token": authn,
            "gfc_session": gfc,
            "session_key": session_key,
        })
        return web.json_response({"ok": True})
    except Exception as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=500)


async def handle_grabfood_status(request: web.Request) -> web.Response:
    """Debug endpoint — returns latest polled order data as JSON."""
    poller: GrabPoller = request.app["poller"]
    return web.json_response(poller.latest or {}, dumps=lambda d, **kw: __import__("json").dumps(d, default=str))


async def handle_novnc_static(request: web.Request) -> web.FileResponse:
    filename = request.match_info.get("filename", "vnc.html")
    filepath = os.path.realpath(os.path.join(NOVNC_DIR, filename))
    _LOGGER.debug("noVNC static: %s -> %s", filename, filepath)
    if not filepath.startswith(NOVNC_DIR) or not os.path.isfile(filepath):
        _LOGGER.warning("noVNC static not found: %s", filepath)
        raise web.HTTPNotFound()
    return web.FileResponse(filepath)


async def handle_websockify(request: web.Request) -> web.WebSocketResponse:
    _LOGGER.debug("WebSocket upgrade request from %s", request.remote)
    _LOGGER.debug("WebSocket headers: %s", dict(request.headers))

    ws = web.WebSocketResponse(protocols=["binary", "base64"])
    await ws.prepare(request)

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(VNC_HOST, VNC_PORT),
            timeout=5.0
        )
        _LOGGER.debug("Connected to VNC server successfully")
    except asyncio.TimeoutError:
        _LOGGER.debug("WebSocket: VNC not available (no login in progress).")
        await ws.close()
        return ws
    except Exception as exc:
        _LOGGER.debug("WebSocket: VNC not available: %s", exc)
        await ws.close()
        return ws

    async def ws_to_vnc():
        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    writer.write(msg.data)
                    await writer.drain()
                elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                    break
        except Exception as e:
            _LOGGER.debug("ws_to_vnc error: %s", e)
        finally:
            writer.close()
            await writer.wait_closed()

    async def vnc_to_ws():
        try:
            while not ws.closed:
                data = await reader.read(4096)
                if not data:
                    break
                await ws.send_bytes(data)
        except Exception as e:
            _LOGGER.debug("vnc_to_ws error: %s", e)
        finally:
            await ws.close()

    await asyncio.gather(ws_to_vnc(), vnc_to_ws())
    _LOGGER.debug("WebSocket session ended")
    return ws


async def on_startup(app: web.Application):
    bridge = Bridge()
    await bridge.start()

    poller = GrabPoller(
        token_store=token_store,
        on_update=bridge.update,
        on_token_expired=bridge.notify_token_expired,
    )
    poller.start()

    app["poller"] = poller
    app["bridge"] = bridge
    _LOGGER.info("GrabPoller and Bridge started.")


async def on_cleanup(app: web.Application):
    app["poller"].stop()
    await app["bridge"].stop()
    _LOGGER.info("GrabPoller and Bridge stopped.")


async def main():
    await token_store.load()

    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    app.router.add_get("/", handle_index)
    app.router.add_post("/login/start", handle_login_start)
    app.router.add_get("/login/status", handle_login_status)
    app.router.add_get("/token/value", handle_token_value)
    app.router.add_post("/token/manual", handle_manual_token)
    app.router.add_get("/grabfood/status", handle_grabfood_status)
    app.router.add_get("/novnc/websockify", handle_websockify)
    app.router.add_get("/novnc/{tail:api/hassio_ingress/.+/novnc/websockify}", handle_websockify)
    app.router.add_get("/novnc/{filename:.*}", handle_novnc_static)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8099)
    await site.start()
    _LOGGER.info("Web server running on port 8099")

    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())