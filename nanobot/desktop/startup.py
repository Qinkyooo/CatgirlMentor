"""Windows current-user login startup; no credentials in the command."""

import os
import subprocess
import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "catgirlmentor"


def launcher_path() -> Path:
    return Path(sys.executable).resolve().parent.parent / "catgirlmentor.exe"


def startup_command(launcher: Path) -> str:
    return subprocess.list2cmdline([str(launcher), "--silent"])


def get_enabled() -> bool:
    if os.name != "nt":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
        return value == startup_command(launcher_path())
    except FileNotFoundError:
        return False


def set_enabled(enabled: bool) -> None:
    if os.name != "nt":
        raise ValueError("登录自启动仅支持 Windows。")
    import winreg
    launcher = launcher_path()
    if enabled and not launcher.is_file():
        raise ValueError("请使用已打包的 catgirlmentor 应用开启登录自启动。")
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, startup_command(launcher))
            else:
                try:
                    winreg.DeleteValue(key, VALUE_NAME)
                except FileNotFoundError:
                    pass
        if get_enabled() != enabled:
            raise OSError("启动项校验失败")
    except OSError as exc:
        raise ValueError("无法更新 Windows 启动项，请检查当前用户权限。") from exc
