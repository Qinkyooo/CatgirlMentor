"""Exercise a packaged manager and real gateway using a local fake model only."""

import argparse
import json
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class ModelStub(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"data": [{"id": "local-smoke"}, {"id": "local-alternate"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        body = json.dumps({"id": "local-smoke", "object": "chat.completion", "created": 1, "model": "local-smoke",
                           "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
                           "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def smoke(python: Path, data: Path) -> None:
    if data.exists():
        raise ValueError("Use a new smoke-test data directory.")
    data.mkdir(parents=True)
    mock = ThreadingHTTPServer(("127.0.0.1", 0), ModelStub)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    output = (data / "manager-output.log").open("wb")
    manager = subprocess.Popen([str(python), "-m", "nanobot.desktop", "--silent", "--no-tray", "--data-dir", str(data)], stdout=output, stderr=output, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        descriptor = data / "manager.json"
        for _ in range(100):
            if descriptor.exists():
                break
            if manager.poll() is not None:
                raise RuntimeError("Manager exited; inspect manager-output.log")
            time.sleep(.2)
        state = json.loads(descriptor.read_text())
        origin = f"http://127.0.0.1:{state['port']}"
        def request(path, payload=None, credential=""):
            body = json.dumps(payload).encode() if payload is not None else None
            req = urllib.request.Request(origin + "/api/" + path, data=body, headers={"Content-Type": "application/json", "Origin": origin, "Authorization": "Bearer " + credential})
            try:
                with urllib.request.urlopen(req, timeout=65) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                raise RuntimeError(f"{path}: {exc.code} {exc.read().decode()}") from exc
        ticket = request("reopen", {}, state["secret"])["url"].split("#")[1]
        token = request("session", {"token": ticket})["token"]
        assert request("status", credential=token)["state"] == "unconfigured"
        models = request("models", {"provider": "custom", "apiKey": "local-test-only", "apiBase": f"http://127.0.0.1:{mock.server_port}/v1"}, token)
        assert models["models"] == ["local-alternate", "local-smoke"]
        assert not (data / "config.json").exists(), "Model lookup must not save the draft"
        request("config", {"provider": "custom", "model": "local-smoke", "apiKey": "local-test-only", "apiBase": f"http://127.0.0.1:{mock.server_port}/v1"}, token)
        request("test", {}, token)
        request("start", {}, token)
        started = request("status", credential=token)
        assert started["state"] == "running", started
        assert started["source"] == "application", started
        chat = request("chat", {}, token)
        with urllib.request.urlopen(chat["url"].split("#")[0], timeout=10) as response:
            html = response.read().decode("utf-8")
            assert '<html lang="zh-CN">' in html
            assert "正在加载 nanobot" in html
        request("restart", {}, token)
        assert request("status", credential=token)["state"] == "running"
        request("stop", {}, token)
        assert request("status", credential=token)["state"] == "stopped"
        request("quit", {}, state["secret"])
        assert manager.wait(timeout=15) == 0
        print("PASS: model discovery without saving, model test, real gateway start/Chinese chat/restart/stop, clean exit")
    finally:
        if manager.poll() is None:
            subprocess.run([str(python), "-m", "nanobot.desktop", "--shutdown", "--silent", "--data-dir", str(data)], timeout=80, creationflags=subprocess.CREATE_NO_WINDOW)
        mock.shutdown()
        output.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args()
    smoke(args.python.resolve(), args.data_dir.resolve())
