import os
import uuid
from pathlib import Path

import pytest

from nanobot.desktop.startup import startup_command


def test_windows_startup_preserves_spaces_and_unicode():
    command = startup_command(Path('C:/用户目录/My App/catgirlmentor.exe'))
    assert command == '"C:\\用户目录\\My App\\catgirlmentor.exe" --silent'


@pytest.mark.skipif(os.name != "nt", reason="Windows registry")
def test_startup_roundtrip_in_isolated_registry_key(tmp_path, monkeypatch):
    import winreg

    import nanobot.desktop.startup as startup
    key = "Software\\catgirlmentor-test-" + uuid.uuid4().hex
    executable = tmp_path / "catgirlmentor.exe"
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
