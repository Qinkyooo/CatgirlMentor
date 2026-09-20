"""Run the manager without requiring an already configured AI gateway."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import webbrowser
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, web
from filelock import FileLock, Timeout

from nanobot.desktop.controller import DesktopController
from nanobot.desktop.server import STATE, create_app
from nanobot.utils.helpers import _write_text_atomic  # pyright: ignore[reportPrivateUsage]


def default_data_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share"))) / "catgirlmentor"


async def run(args: argparse.Namespace) -> None:
    data: Path = args.data_dir.resolve()
    data.mkdir(parents=True, exist_ok=True)
    descriptor = data / "manager.json"
    lock = FileLock(data / "manager.lock")
    try:
        lock.acquire(timeout=0)
    except Timeout:
        if args.silent and not args.shutdown:
            return
        for _ in range(1 if args.shutdown else 30):
            try:
                saved = json.loads(descriptor.read_text(encoding="utf-8"))
                port = saved["port"]
                if type(port) is not int or not 1 <= port <= 65535:
                    raise ValueError("Invalid manager port")
                origin = f"http://127.0.0.1:{port}"
                async with ClientSession() as client:
                    async with client.post(origin + ("/api/quit" if args.shutdown else "/api/reopen"), json={}, headers={"Authorization": "Bearer " + saved["secret"]}, timeout=ClientTimeout(total=60 if args.shutdown else 3)) as response:
                        response.raise_for_status()
                        result = await response.json()
                        if args.shutdown:
                            for _ in range(100):
                                if not descriptor.exists():
                                    return
                                await asyncio.sleep(0.1)
                            raise RuntimeError("管理进程未能退出。")
                        if not result["url"].startswith(origin + "/#"):
                            raise ValueError("Invalid manager URL")
                        webbrowser.open(result["url"])
                        return
            except Exception:
                await asyncio.sleep(0.2)
        raise RuntimeError("管理实例正在运行但无法连接，请稍后重试。")

    if args.shutdown:
        try:
            DesktopController(data).close()
        finally:
            lock.release()
        return

    controller = DesktopController(data)
    app = create_app(controller)
    runner = web.AppRunner(app, access_log=None)
    icon = task = None
    try:
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", args.port)
        await site.start()
        port = runner.addresses[0][1]
        state = app[STATE]
        state.origin = f"http://127.0.0.1:{port}"
        _write_text_atomic(descriptor, json.dumps({"port": port, "secret": state.instance_secret, "pid": os.getpid()}))
        def open_manager() -> None:
            webbrowser.open(state.origin + "/#" + state.bootstrap())
        if not args.no_tray:
            from nanobot.desktop.tray import start_tray
            icon, task = start_tray(controller, asyncio.get_running_loop(), open_manager, state.shutdown)
        if not args.silent:
            open_manager()
        elif controller.snapshot()["state"] == "stopped":
            try:
                await asyncio.to_thread(controller.control, "start")
            except ValueError:
                pass  # Tray and manager remain available to explain startup failure.
        await state.shutdown.wait()
    finally:
        if task:
            task.cancel()
        if icon:
            icon.stop()
        await runner.cleanup()
        descriptor.unlink(missing_ok=True)
        lock.release()


def main() -> None:
    parser = argparse.ArgumentParser(description="catgirlmentor local manager")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--silent", action="store_true")
    parser.add_argument("--shutdown", action="store_true", help="Graceful installer shutdown")
    parser.add_argument("--no-tray", action="store_true", help="Headless diagnostics")
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        args.data_dir.mkdir(parents=True, exist_ok=True)
        _write_text_atomic(args.data_dir / "manager-error.txt", f"{time.ctime()} {type(exc).__name__}: {exc}")
        if os.name == "nt" and not args.silent:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, "管理入口启动失败，请查看数据目录中的 manager-error.txt。", "CatgirlMentor", 0x10)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
