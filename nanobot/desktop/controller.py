"""Path-scoped desktop configuration and owned gateway lifecycle."""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import socket
import sys
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import httpx

from nanobot import __version__
from nanobot.channels.websocket.runtime import WebSocketConfig
from nanobot.config.loader import resolve_config_env_vars
from nanobot.config.schema import Config
from nanobot.gateway import GatewayInstance, GatewayRuntime, GatewayStatus
from nanobot.providers.factory import make_provider, validate_provider_setup
from nanobot.providers.registry import PROVIDERS
from nanobot.utils.helpers import _write_text_atomic  # pyright: ignore[reportPrivateUsage]
from nanobot.webui.settings_models import provider_models_payload, update_provider_settings
from nanobot.webui.settings_services import WebUISettingsConfig


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class DesktopController:
    """A manager can stay available even with missing or invalid agent config."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir.resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.preferences = self.data_dir / "desktop.json"
        self.config_path = self.data_dir / "config.json"
        if self.preferences.exists():
            try:
                saved = json.loads(self.preferences.read_text(encoding="utf-8"))
                if saved.get("use_existing") is True:
                    self.config_path = Path.home() / ".nanobot" / "config.json"
            except (ValueError, OSError, AttributeError):
                pass
        self._bind()
        self.busy: str | None = None
        self.error = ""
        self._transition = threading.Lock()
        self.owned: tuple[int | None, str | None] | None = None
        self._owner_path = self.data_dir / "owned-gateway.json"
        try:
            owner = json.loads(self._owner_path.read_text(encoding="utf-8"))
            if owner.get("config") == str(self.config_path):
                self.owned = (int(owner["pid"]), owner["started_at"])
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass

    def _bind(self) -> None:
        self.settings = WebUISettingsConfig(self.config_path)
        self.instance = GatewayInstance.resolve(config_path=self.config_path)
        executable = Path(sys.executable)
        if executable.name.lower() == "pythonw.exe":
            executable = executable.with_name("python.exe")
        self.runtime = GatewayRuntime(paths=self.instance.paths, python_executable=str(executable))

    def select_config(self, use_existing: bool) -> None:
        with self._mutation():
            self._select_config(use_existing)

    @contextmanager
    def _mutation(self) -> Generator[None]:
        if not self._transition.acquire(blocking=False):
            raise ValueError("其他操作正在进行，请稍后重试。")
        try:
            yield
        finally:
            self._transition.release()

    def _select_config(self, use_existing: bool) -> None:
        if self.runtime.status().running or self.busy:
            raise ValueError("请先停止当前后台，再切换数据配置。")
        path = Path.home() / ".nanobot" / "config.json" if use_existing else self.data_dir / "config.json"
        if use_existing and not path.is_file():
            raise ValueError("未找到已有 nanobot 配置。")
        self.config_path = path
        self._bind()
        self.owned = None
        self.error = ""
        _write_text_atomic(self.preferences, json.dumps({"use_existing": use_existing}))

    def redact(self, text: str) -> str:
        # Read raw config so unresolved ${ENV} values do not prevent redaction.
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
            def walk(value: object, key: str = "") -> None:
                nonlocal text
                if isinstance(value, dict):
                    for name, child in cast(dict[object, object], value).items():
                        walk(child, str(name))
                elif isinstance(value, list):
                    for child in cast(list[object], value):
                        walk(child, key)
                elif isinstance(value, str) and any(word in key.lower() for word in ("key", "secret", "token", "password")):
                    if value:
                        text = text.replace(value, "[已隐藏]")
                        for variable in re.findall(r"\$\{([^}]+)\}", value):
                            resolved = os.environ.get(variable)
                            if resolved:
                                text = text.replace(resolved, "[已隐藏]")
            walk(raw)
        except (OSError, ValueError):
            return "配置无法读取，暂不展示日志以保护凭据。"
        text = re.sub(r"(?i)(bearer\s+)[^\s\"',]+", r"\1[已隐藏]", text)
        return re.sub(r"(?i)((?:api[_-]?key|token|secret|password)[\"']?\s*[:=]\s*)[^\s,}]+", r"\1[已隐藏]", text)

    def snapshot(self) -> dict[str, object]:
        status = self.runtime.status()
        state = "running" if status.running and status.ready else "starting" if status.running else "stopped"
        model: dict[str, object] = {}
        config_error = ""
        chat_url = ""
        if not self.config_path.exists():
            state = "unconfigured" if not status.running else state
        else:
            try:
                config = self.settings.load()
                provider_name = config.agents.defaults.provider
                provider = getattr(config.providers, provider_name, None)
                model = {"provider": provider_name, "model": config.agents.defaults.model,
                         "apiBase": provider.api_base or "" if provider else "",
                         "hasKey": bool(provider and provider.api_key),
                         "preset": config.agents.defaults.model_preset}
                ws = WebSocketConfig.model_validate(getattr(config.channels, "websocket", {}) or {})
                if ws.enabled and ws.host in {"127.0.0.1", "localhost", "::1"}:
                    chat_url = f"http://127.0.0.1:{ws.port}"
                try:
                    validate_provider_setup(config)
                except (ValueError, RuntimeError):
                    if not status.running:
                        state = "unconfigured"
            except Exception:
                config_error = "配置文件无法解析。可打开数据目录修复，或备份后重置。"
                if not status.running:
                    state = "failed"
        if self.error and not status.ready:
            state = "failed"
        return {"brand": "CatgirlMentor", "version": __version__, "state": self.busy or state,
                "error": self.error or config_error, "model": model,
                "dataDir": str(self.config_path.parent), "configPath": str(self.config_path),
                "existingAvailable": (Path.home() / ".nanobot" / "config.json").is_file(),
                "useExisting": self.config_path != self.data_dir / "config.json",
                "connections": self.connection_summaries(),
                "pid": status.pid, "startedAt": status.started_at,
                "source": "application" if self._owns(status) else "external" if status.running else "none",
                "chatUrl": chat_url, "providers": [p.name for p in PROVIDERS if not p.is_oauth and p.name != "bedrock"]}

    def connection_summaries(self) -> dict[str, dict[str, object]]:
        try:
            config = self.settings.load()
        except (ValueError, OSError):
            return {}
        result: dict[str, dict[str, object]] = {}
        for spec in PROVIDERS:
            provider = getattr(config.providers, spec.name, None)
            if provider is not None:
                result[spec.name] = {"apiBase": provider.api_base or "", "hasKey": bool(provider.api_key)}
        return result

    def configure(self, payload: dict[str, object]) -> None:
        with self._mutation():
            self._configure(payload)

    @staticmethod
    def _connection_query(payload: dict[str, object]) -> dict[str, list[str]]:
        provider = payload.get("provider")
        if not isinstance(provider, str) or provider not in {p.name for p in PROVIDERS if not p.is_oauth}:
            raise ValueError("请选择有效的模型提供商。")
        base = payload.get("apiBase", "")
        key = payload.get("apiKey", "")
        if not isinstance(base, str) or not isinstance(key, str) or len(base) > 2048 or len(key) > 8192:
            raise ValueError("连接字段格式无效。")
        base = base.strip()
        if base:
            parsed = urlsplit(base)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("API 地址必须是有效的 HTTP(S) 地址，不能包含凭据、查询参数或片段。")
        if provider == "custom" and not base:
            raise ValueError("自定义提供商需要 API 地址。")
        query = {"provider": [provider], "api_base": [base]}
        if key.strip():
            query["api_key"] = [key.strip()]
        return query

    def list_models(self, payload: dict[str, object]) -> dict[str, object]:
        query = self._connection_query(payload)
        config = self.settings.load()
        name = query["provider"][0]
        saved = getattr(config.providers, name, None)
        if saved is not None and saved.api_key and "api_key" not in query:
            spec = next(p for p in PROVIDERS if p.name == name)
            default_base = spec.default_api_base or ("https://api.openai.com/v1" if name == "openai" else "")
            previous_base = (saved.api_base or default_base).rstrip("/")
            draft_base = (query["api_base"][0] or default_base).rstrip("/")
            if previous_base != draft_base:
                raise ValueError("API 地址已变化，请重新输入该服务商的密钥后获取模型。")
        # Apply draft fields only to this in-memory copy, never persist a model lookup.
        update_provider_settings(config, query)
        result = provider_models_payload(config, query, http_get=httpx.get)
        models = sorted({row["id"] for row in result.get("models", [])
                         if isinstance(row.get("id"), str) and 0 < len(row["id"]) <= 200})[:1000]
        if models:
            message = "已获取可选模型，可从列表中选择，也可以手动输入。"
            if result.get("catalog_kind") == "builtin":
                message = "显示服务商内置模型列表，也可以手动输入其他模型名称。"
        else:
            message = {
                "not_configured": "请检查 API 密钥后重试，也可以手动输入模型名称。",
                "missing_api_base": "请先填写 API 地址，也可以手动输入模型名称。",
                "unsupported": "该服务商暂不支持获取模型列表，请手动输入模型名称。",
                "available": "服务商未返回可选模型，请手动输入模型名称。",
            }.get(result.get("status", ""), "无法获取模型列表，请检查地址和网络，或手动输入模型名称。")
        return {"models": models, "message": message}

    def _configure(self, payload: dict[str, object]) -> None:
        if self.busy:
            raise ValueError("后台正在切换状态，请稍后保存。")
        query = self._connection_query(payload)
        provider = query["provider"][0]
        model = payload.get("model")
        if not isinstance(model, str) or not model.strip() or len(model) > 200:
            raise ValueError("请填写模型名称（最多 200 字符）。")

        fresh = not self.config_path.exists()
        def mutate(config: Config) -> None:
            update_provider_settings(config, query)
            config.agents.defaults.model = model.strip()
            config.agents.defaults.provider = provider
            config.agents.defaults.model_preset = None
            if fresh:
                config.agents.defaults.workspace = str(self.config_path.parent / "workspace")
                config.gateway.port = free_port()
            ws = WebSocketConfig.model_validate(getattr(config.channels, "websocket", {}) or {})
            if fresh or not ws.enabled:
                ws.host = "127.0.0.1"
                ws.port = free_port()
            ws.enabled = True
            ws.websocket_requires_token = True
            if not ws.token_issue_secret and not ws.token:
                ws.token_issue_secret = secrets.token_urlsafe(32)
            setattr(config.channels, "websocket", ws.model_dump(by_alias=True))
        self.settings.update(mutate)
        self.error = ""

    async def test_connection(self) -> None:
        config = resolve_config_env_vars(self.settings.load(), config_path=self.config_path)
        validate_provider_setup(config)
        # Test only the selected primary, never silently succeed via a fallback.
        config.agents.defaults.fallback_models = []
        provider = make_provider(config)
        try:
            response = await asyncio.wait_for(provider.chat(messages=[{"role": "user", "content": "Reply with OK."}], max_tokens=16), timeout=30)
            if response.finish_reason == "error":
                raise ValueError(f"连接失败（{response.error_status_code or response.error_kind or '模型错误'}），请检查凭据、地址和模型。")
        finally:
            close = getattr(provider, "close", None)
            if close:
                result = close()
                if asyncio.iscoroutine(result):
                    await result

    def chat_url(self) -> str:
        from nanobot.cli.webui_support import _webui_browser_url
        config = resolve_config_env_vars(self.settings.load(), config_path=self.config_path)
        ws = WebSocketConfig.model_validate(getattr(config.channels, "websocket", {}) or {})
        if not ws.enabled or ws.host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("桌面聊天入口需要启用本机 WebUI。")
        return _webui_browser_url(config)

    def _owns(self, status: GatewayStatus) -> bool:
        return bool(status.running and self.owned and self.owned == (status.pid, status.started_at))

    def control(self, action: str) -> None:
        if action not in {"start", "stop", "restart"}:
            raise ValueError("不支持的后台操作。")
        if not self._transition.acquire(blocking=False):
            raise ValueError("后台正在切换状态，请稍候。")
        self.busy = "stopping" if action == "stop" else "starting"
        try:
            status = self.runtime.status()
            if status.running and not self._owns(status):
                if action == "start":
                    return
                raise ValueError("当前后台由外部 CLI 启动，请在原入口停止，避免影响其他会话。")
            if action == "stop":
                if not status.running:
                    self.error = ""
                    return
                result = self.runtime.stop(expected_identity=self.owned)
                if not result.ok:
                    raise ValueError(result.message)
                self.owned = None
                self._owner_path.unlink(missing_ok=True)
                self.error = ""
                return
            config = resolve_config_env_vars(self.settings.load(), config_path=self.config_path)
            validate_provider_setup(config)
            options = self.instance.start_options(port=config.gateway.port)
            result = self.runtime.restart(options, expected_identity=self.owned) if status.running else self.runtime.start_background(options)
            if not result.ok:
                if result.message == "gateway_already_running":
                    return
                raise ValueError(result.message)
            self.owned = (result.status.pid, result.status.started_at)
            _write_text_atomic(self._owner_path, json.dumps({"pid": result.status.pid, "started_at": result.status.started_at, "config": str(self.config_path)}))
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                status = self.runtime.status()
                if status.running and self.owned and status.started_at == self.owned[1] and status.launch_mode == "background":
                    # GatewayRuntime preserves its launch timestamp when a Windows venv
                    # launcher hands its child the PID. A new external launch gets a new one.
                    self.owned = (status.pid, status.started_at)
                    _write_text_atomic(self._owner_path, json.dumps({"pid": status.pid, "started_at": status.started_at, "config": str(self.config_path)}))
                if status.running and not self._owns(status):
                    raise ValueError("后台实例已被外部入口替换，请在原入口管理。")
                if status.running and status.ready:
                    self.error = ""
                    return
                if not status.running:
                    break
                time.sleep(0.3)
            raise ValueError("后台未能就绪，请查看日志。可能是端口占用或配置问题。")
        except Exception as exc:
            self.error = self.redact(str(exc))
            raise ValueError(self.error) from exc
        finally:
            self.busy = None
            self._transition.release()

    def reset_config(self) -> str:
        with self._mutation():
            return self._reset_config()

    def _reset_config(self) -> str:
        if self.runtime.status().running or self.busy:
            raise ValueError("请先停止后台。")
        backup = self.config_path.with_name(f"config.backup-{time.time_ns()}.json")
        if self.config_path.exists():
            self.config_path.rename(backup)
        self.error = ""
        return str(backup)

    def logs(self) -> str:
        try:
            with self.runtime.paths.log_path.open("rb") as handle:
                handle.seek(0, 2)
                handle.seek(max(0, handle.tell() - 65536))
                text = handle.read(65536).decode("utf-8", errors="replace")
        except FileNotFoundError:
            return "暂无后台日志。"
        return self.redact("\n".join(text.splitlines()[-300:]))

    def close(self) -> None:
        with self._transition:
            if self._owns(self.runtime.status()):
                result = self.runtime.stop(expected_identity=self.owned)
                if not result.ok:
                    raise RuntimeError("后台停止失败，请重试退出。")
                self.owned = None
                self._owner_path.unlink(missing_ok=True)
