"""Native tray menu using the approved encyclopedia symbol."""

import asyncio
import importlib
import threading
import webbrowser
from pathlib import Path
from typing import Callable, Protocol

from nanobot.desktop.controller import DesktopController

LABELS = {"running": "运行中", "stopped": "已停止", "starting": "启动中",
          "stopping": "停止中", "failed": "启动失败", "unconfigured": "待配置"}


class TrayIcon(Protocol):
    """The small public API used from the untyped pystray SDK."""

    title: str

    def run(self) -> None: ...
    def stop(self) -> None: ...
    def update_menu(self) -> None: ...


def start_tray(controller: DesktopController, loop: asyncio.AbstractEventLoop,
               open_manager: Callable[[], None], shutdown: asyncio.Event) -> tuple[TrayIcon, asyncio.Task[None]]:
    pystray = importlib.import_module("pystray")
    from PIL import Image

    current = {"state": "unconfigured", "source": "none"}

    async def operate(action: str) -> None:
        try:
            if action == "exit":
                await asyncio.to_thread(controller.close)
                shutdown.set()
            elif action == "chat":
                webbrowser.open(controller.chat_url())
            else:
                await asyncio.to_thread(controller.control, action)
        except Exception:
            open_manager()

    def dispatch(action: str) -> Callable[..., None]:
        def callback(*_args: object) -> None:
            asyncio.run_coroutine_threadsafe(operate(action), loop)
        return callback

    def open_page(*_args: object) -> None:
        loop.call_soon_threadsafe(open_manager)

    def chat_enabled(_item: object) -> bool:
        return current["state"] == "running"

    def start_enabled(_item: object) -> bool:
        return current["state"] in {"stopped", "failed"}

    def stop_enabled(_item: object) -> bool:
        return current["state"] in {"running", "failed"} and current["source"] == "application"

    icon: TrayIcon = pystray.Icon("CatgirlMentor", Image.open(Path(__file__).parent / "static" / "tray-32.png"), "CatgirlMentor · 待配置", menu=pystray.Menu(
        pystray.MenuItem("打开管理页", open_page, default=True),
        pystray.MenuItem("打开聊天界面", dispatch("chat"), enabled=chat_enabled),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("启动后台", dispatch("start"), enabled=start_enabled),
        pystray.MenuItem("停止后台", dispatch("stop"), enabled=stop_enabled),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出应用", dispatch("exit")),
    ))
    thread = threading.Thread(target=icon.run, daemon=True, name="CatgirlMentor-tray")
    thread.start()

    async def update() -> None:
        while not shutdown.is_set():
            try:
                snapshot = await asyncio.to_thread(controller.snapshot)
                current.update({"state": str(snapshot["state"]), "source": str(snapshot["source"])})
                icon.title = "CatgirlMentor · " + LABELS.get(current["state"], "正在检查")
                icon.update_menu()
            except Exception:
                icon.title = "CatgirlMentor · 状态不可用"
            await asyncio.sleep(2)
    return icon, asyncio.create_task(update())
