"""Authenticated loopback management server, independent of the gateway."""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from aiohttp import web

from nanobot.desktop.controller import DesktopController

STATIC = Path(__file__).parent / "static"


@dataclass
class ManagerState:
    bootstrap: Callable[[], str]
    origin: str = "http://127.0.0.1:0"
    instance_secret: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    shutdown: asyncio.Event = field(default_factory=asyncio.Event)


STATE = web.AppKey("manager_state", ManagerState)


async def request_object(request: web.Request) -> dict[str, object]:
    payload: object = await request.json()
    if not isinstance(payload, dict):
        raise ValueError("请求内容必须是对象。")
    # JSON object keys are always strings; field values are validated by each action.
    return cast(dict[str, object], payload)


def create_app(controller: DesktopController) -> web.Application:
    tickets: dict[str, float] = {}
    sessions: dict[str, float] = {}

    def bootstrap() -> str:
        now = time.monotonic()
        for key in list(tickets):
            if tickets[key] < now:
                del tickets[key]
        if len(tickets) >= 16:
            del tickets[next(iter(tickets))]
        token = secrets.token_urlsafe(32)
        tickets[token] = now + 60
        return token

    @web.middleware
    async def guard(request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]) -> web.StreamResponse:
        expected = urlsplit(state.origin)
        if request.host != expected.netloc:
            raise web.HTTPForbidden(text="Invalid host")
        origin = request.headers.get("Origin")
        if origin and origin != state.origin:
            raise web.HTTPForbidden(text="Invalid origin")
        local_reopen = request.path in {"/api/reopen", "/api/quit"} and secrets.compare_digest(request.headers.get("Authorization", ""), "Bearer " + state.instance_secret)
        if request.method not in {"GET", "HEAD"} and origin != state.origin and not local_reopen:
            raise web.HTTPForbidden(text="Origin required")
        if request.path.startswith("/api/") and request.path != "/api/session" and not local_reopen:
            credential = request.headers.get("Authorization", "").removeprefix("Bearer ")
            if sessions.get(credential, 0) < time.monotonic():
                raise web.HTTPUnauthorized()
        try:
            response = await handler(request)
        except (ValueError, RuntimeError) as exc:
            response = web.json_response({"error": controller.redact(str(exc))}, status=400)
        except asyncio.TimeoutError:
            response = web.json_response({"error": "连接超时，请检查网络和 API 地址。"}, status=400)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
                                 "Content-Security-Policy": "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        return response

    app = web.Application(middlewares=[guard], client_max_size=16384)
    state = ManagerState(bootstrap)
    app[STATE] = state

    async def session(request: web.Request) -> web.Response:
        payload = await request_object(request)
        token = payload.get("token")
        if not isinstance(token, str) or tickets.pop(token, 0) < time.monotonic():
            raise web.HTTPUnauthorized()
        now = time.monotonic()
        for old in list(sessions):
            if sessions[old] < now:
                del sessions[old]
        if len(sessions) >= 32:
            del sessions[next(iter(sessions))]
        credential = secrets.token_urlsafe(32)
        sessions[credential] = now + 43200
        return web.json_response({"token": credential})

    async def reopen(request: web.Request) -> web.Response:
        return web.json_response({"url": state.origin + "/#" + bootstrap()})

    async def quit_instance(request: web.Request) -> web.Response:
        await asyncio.to_thread(controller.close)
        state.shutdown.set()
        return web.json_response({"ok": True})

    async def status(request: web.Request) -> web.Response:
        return web.json_response(await asyncio.to_thread(controller.snapshot))

    async def logs(request: web.Request) -> web.Response:
        return web.json_response({"text": await asyncio.to_thread(controller.logs)})

    async def mutate(request: web.Request) -> web.Response:
        action = request.match_info["action"]
        payload = await request_object(request)
        if action == "config":
            await asyncio.to_thread(controller.configure, payload)
        elif action == "models":
            return web.json_response(await asyncio.to_thread(controller.list_models, payload))
        elif action == "select-config":
            use_existing = payload.get("useExisting")
            if not isinstance(use_existing, bool):
                raise ValueError("配置选择无效。")
            await asyncio.to_thread(controller.select_config, use_existing)
        elif action == "reset-config":
            if payload.get("confirm") is not True:
                raise ValueError("请确认备份并重置配置。")
            return web.json_response({"backup": await asyncio.to_thread(controller.reset_config)})
        elif action == "test":
            await controller.test_connection()
        elif action in {"start", "stop", "restart"}:
            await asyncio.to_thread(controller.control, action)
        elif action == "chat":
            snapshot = await asyncio.to_thread(controller.snapshot)
            if snapshot["state"] != "running" or not snapshot["chatUrl"]:
                raise ValueError("聊天界面尚未就绪。")
            return web.json_response({"url": controller.chat_url()})
        elif action == "exit":
            await asyncio.to_thread(controller.close)
            state.shutdown.set()
        elif action == "open-data":
            import os
            if os.name != "nt":
                raise ValueError("仅 Windows 支持打开目录。")
            os.startfile(controller.config_path.parent)
        elif action == "startup":
            from nanobot.desktop.startup import set_enabled
            enabled = payload.get("enabled")
            if not isinstance(enabled, bool):
                raise ValueError("自启动开关无效。")
            await asyncio.to_thread(set_enabled, enabled)
        else:
            raise web.HTTPNotFound()
        return web.json_response({"ok": True})

    async def startup_status(request: web.Request) -> web.Response:
        from nanobot.desktop.startup import get_enabled
        return web.json_response({"enabled": get_enabled()})

    async def static(request: web.Request) -> web.FileResponse:
        name = request.match_info.get("name") or "index.html"
        if name not in {"index.html", "app.css", "app.js", "desktop.ico"}:
            raise web.HTTPNotFound()
        return web.FileResponse(STATIC / name)

    app.router.add_post("/api/session", session)
    app.router.add_post("/api/reopen", reopen)
    app.router.add_post("/api/quit", quit_instance)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/logs", logs)
    app.router.add_get("/api/startup", startup_status)
    app.router.add_post("/api/{action}", mutate)
    app.router.add_get("/", static)
    app.router.add_get("/{name}", static)
    return app
