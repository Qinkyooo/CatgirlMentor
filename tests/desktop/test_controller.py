from pathlib import Path

import pytest

from nanobot.desktop.controller import DesktopController


def test_model_discovery_uses_unsaved_fields_without_writing_config(tmp_path, monkeypatch):
    import httpx

    import nanobot.desktop.controller as module
    app = DesktopController(tmp_path)
    requests = []
    def get(url, **kwargs):
        requests.append((url, kwargs))
        return httpx.Response(200, json={"data": [{"id": "model-b"}, {"id": "model-a"}]}, request=httpx.Request("GET", url))
    monkeypatch.setattr(module.httpx, "get", get)
    result = app.list_models({"provider": "custom", "apiBase": "http://localhost:9999/v1", "apiKey": "draft-key"})
    assert result["models"] == ["model-a", "model-b"]
    assert requests[0][0] == "http://localhost:9999/v1/models"
    assert requests[0][1]["headers"]["Authorization"] == "Bearer draft-key"
    assert not app.config_path.exists()


def test_model_discovery_preserves_saved_key_and_sanitizes_failure(tmp_path, monkeypatch):
    import httpx

    import nanobot.desktop.controller as module
    app = DesktopController(tmp_path)
    app.configure({"provider": "custom", "apiBase": "http://localhost:9999/v1", "apiKey": "${DESKTOP_LIST_KEY}", "model": "existing"})
    before = app.config_path.read_bytes()
    monkeypatch.setenv("DESKTOP_LIST_KEY", "saved-key")
    def get(url, **kwargs):
        assert kwargs["headers"]["Authorization"] == "Bearer saved-key"
        raise httpx.ConnectError("untrusted message saved-key")
    monkeypatch.setattr(module.httpx, "get", get)
    result = app.list_models({"provider": "custom", "apiBase": "http://localhost:9999/v1", "apiKey": ""})
    assert result["models"] == []
    assert "saved-key" not in str(result)
    assert "手动" in result["message"]
    assert app.config_path.read_bytes() == before


def test_model_discovery_rejects_invalid_destination(tmp_path):
    app = DesktopController(tmp_path)
    with pytest.raises(ValueError):
        app.list_models({"provider": "custom", "apiBase": "file:///secret"})


def test_model_lookup_never_sends_saved_key_to_a_changed_address(tmp_path, monkeypatch):
    import nanobot.desktop.controller as module
    app = DesktopController(tmp_path)
    app.configure({"provider": "openai", "apiKey": "saved-private-key", "model": "test"})
    def forbidden(*args, **kwargs):
        pytest.fail("Saved credential must not go to a new endpoint")
    monkeypatch.setattr(module.httpx, "get", forbidden)
    with pytest.raises(ValueError, match="重新输入"):
        app.list_models({"provider": "openai", "apiBase": "http://localhost:9999/v1", "apiKey": ""})


def test_first_run_is_independent_and_unconfigured(tmp_path):
    app = DesktopController(tmp_path)
    assert app.snapshot()["state"] == "unconfigured"
    assert app.config_path == tmp_path / "config.json"
    assert not app.config_path.exists()


def test_configure_preserves_secret_and_uses_local_webui(tmp_path):
    app = DesktopController(tmp_path)
    app.configure({"provider": "deepseek", "model": "deepseek-chat", "apiKey": "secret-example"})
    app.configure({"provider": "deepseek", "model": "deepseek-chat", "apiKey": ""})
    config = app.settings.load()
    assert config.providers.deepseek.api_key == "secret-example"
    assert config.agents.defaults.workspace == str(tmp_path / "workspace")
    assert config.channels.websocket["host"] == "127.0.0.1"
    assert "secret-example" not in str(app.snapshot())
    assert app.snapshot()["state"] == "stopped"


@pytest.mark.parametrize("payload", [
    {"provider": "missing", "model": "x"},
    {"provider": "deepseek", "model": ""},
    {"provider": "custom", "model": "x", "apiBase": "file:///etc/test"},
    {"provider": "custom", "model": "x", "apiBase": "https://user:secret@example.com"},
])
def test_invalid_config_never_written(tmp_path, payload):
    app = DesktopController(tmp_path)
    with pytest.raises(ValueError):
        app.configure(payload)
    assert not app.config_path.exists()


def test_corrupt_config_keeps_manager_available(tmp_path):
    app = DesktopController(tmp_path)
    app.config_path.write_text("broken-json", encoding="utf-8")
    assert app.snapshot()["state"] == "failed"
    backup = app.reset_config()
    assert Path(backup).read_text() == "broken-json"
    assert app.snapshot()["state"] == "unconfigured"


def test_logs_redact_secrets_and_bound_output(tmp_path):
    app = DesktopController(tmp_path)
    app.configure({"provider": "deepseek", "model": "deepseek-chat", "apiKey": "secret-example"})
    app.runtime.paths.log_path.parent.mkdir(parents=True, exist_ok=True)
    app.runtime.paths.log_path.write_text("Authorization: Bearer secret-example\n" * 1000)
    logs = app.logs()
    assert "secret-example" not in logs
    assert len(logs.splitlines()) <= 300


def test_exit_does_not_stop_external_gateway(tmp_path, monkeypatch):
    app = DesktopController(tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail("external gateway must not be stopped")
    monkeypatch.setattr(app.runtime, "stop", forbidden)
    app.close()


def test_racing_external_gateway_is_not_claimed(tmp_path, monkeypatch):
    from dataclasses import replace

    from nanobot.gateway import RuntimeResult
    app = DesktopController(tmp_path)
    app.configure({"provider": "deepseek", "model": "deepseek-chat", "apiKey": "test-only"})
    stopped = app.runtime.status()
    external = replace(stopped, running=True, pid=98765, started_at="external-start", ready=True)
    current = [stopped]
    monkeypatch.setattr(app.runtime, "status", lambda: current[0])
    def race(options):
        current[0] = external
        return RuntimeResult(False, "gateway_already_running", external)
    monkeypatch.setattr(app.runtime, "start_background", race)
    app.control("start")
    assert app.snapshot()["source"] == "external"
    assert app.owned is None


def test_unready_owned_gateway_remains_recoverable(tmp_path, monkeypatch):
    from dataclasses import replace
    app = DesktopController(tmp_path)
    status = replace(app.runtime.status(), running=True, pid=123, started_at="one", ready=False)
    app.owned = (123, "one")
    app.error = "startup timed out"
    monkeypatch.setattr(app.runtime, "status", lambda: status)
    assert app.snapshot()["state"] == "failed"
    assert app.snapshot()["source"] == "application"


def test_gateway_rejects_changed_identity_before_stop(tmp_path, monkeypatch):
    from dataclasses import replace
    app = DesktopController(tmp_path)
    status = replace(app.runtime.status(), running=True, pid=222, started_at="replacement")
    monkeypatch.setattr(app.runtime, "status", lambda: status)
    result = app.runtime.stop(expected_identity=(111, "original"))
    assert not result.ok
    assert result.message == "gateway_identity_changed"


def test_background_claim_preserves_start_identity(tmp_path, monkeypatch):
    import os

    from nanobot.gateway import GatewayStartOptions
    app = DesktopController(tmp_path)
    app.runtime.paths.run_dir.mkdir(parents=True, exist_ok=True)
    record = {"pid": os.getpid(), "started_at": "original-start", "launch_mode": "background", **app.runtime.process_identity_record(os.getpid())}
    app.runtime._write_state(record)
    app.runtime._claim_current_process(GatewayStartOptions(port=19000))
    assert app.runtime._read_state()["started_at"] == "original-start"


@pytest.mark.asyncio
async def test_connection_resolves_environment_key(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import nanobot.desktop.controller as module
    app = DesktopController(tmp_path)
    app.configure({"provider": "deepseek", "model": "deepseek-chat", "apiKey": "${CATGIRLMENTOR_TEST_KEY}"})
    monkeypatch.setenv("CATGIRLMENTOR_TEST_KEY", "resolved-value")
    received = []
    class Provider:
        async def chat(self, **kwargs):
            return SimpleNamespace(finish_reason="stop")
    def factory(config):
        received.append(config.providers.deepseek.api_key)
        return Provider()
    monkeypatch.setattr(module, "make_provider", factory)
    await app.test_connection()
    assert received == ["resolved-value"]
    assert app.settings.load().providers.deepseek.api_key == "${CATGIRLMENTOR_TEST_KEY}"


def test_chat_entry_resolves_existing_environment_secret(tmp_path, monkeypatch):
    app = DesktopController(tmp_path)
    app.configure({"provider": "deepseek", "model": "deepseek-chat", "apiKey": "test-only"})
    def set_secret(config):
        config.channels.websocket["tokenIssueSecret"] = "${CATGIRLMENTOR_WEBUI_TEST}"
    app.settings.update(set_secret)
    monkeypatch.setenv("CATGIRLMENTOR_WEBUI_TEST", "resolved-webui-secret")
    assert "bootstrapSecret=resolved-webui-secret" in app.chat_url()
    assert app.settings.load().channels.websocket["tokenIssueSecret"] == "${CATGIRLMENTOR_WEBUI_TEST}"
