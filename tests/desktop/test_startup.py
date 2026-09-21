import os
import uuid
from pathlib import Path

import pytest

from nanobot.desktop.startup import startup_command


def test_windows_startup_preserves_spaces_and_unicode():
    command = startup_command(Path('C:/用户目录/My App/CatgirlMentor.exe'))
    assert command == '"C:\\用户目录\\My App\\CatgirlMentor.exe" --silent'


@pytest.mark.skipif(os.name != "nt", reason="Windows registry")
def test_startup_roundtrip_in_isolated_registry_key(tmp_path, monkeypatch):
    import winreg

    import nanobot.desktop.startup as startup
    key = "Software\\CatgirlMentor-test-" + uuid.uuid4().hex
    executable = tmp_path / "CatgirlMentor.exe"
    executable.touch()
    monkeypatch.setattr(startup, "RUN_KEY", key)
    monkeypatch.setattr(startup, "launcher_path", lambda: executable)
    try:
        startup.set_enabled(True)
        assert startup.get_enabled()
        startup.set_enabled(False)
        assert not startup.get_enabled()
    finally:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)


@pytest.mark.skipif(os.name != "nt", reason="Windows registry")
@pytest.mark.parametrize("quoted", [False, True])
def test_legacy_startup_remains_enabled_and_normalizes_name(tmp_path, monkeypatch, quoted):
    import winreg

    import nanobot.desktop.startup as startup
    key = "Software\\CatgirlMentor-test-" + uuid.uuid4().hex
    executable = tmp_path / "CatgirlMentor.exe"
    executable.touch()
    monkeypatch.setattr(startup, "RUN_KEY", key)
    monkeypatch.setattr(startup, "launcher_path", lambda: executable)
    legacy = str(executable).lower()
    command = f'"{legacy}" --silent' if quoted else startup_command(Path(legacy))
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as handle:
            winreg.SetValueEx(handle, "catgirlmentor", 0, winreg.REG_SZ, command)
        assert startup.get_enabled()
        startup.set_enabled(True)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
            assert winreg.EnumValue(handle, 0)[:2] == ("CatgirlMentor", startup_command(executable))
        startup.set_enabled(False)
        assert not startup.get_enabled()
    finally:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
