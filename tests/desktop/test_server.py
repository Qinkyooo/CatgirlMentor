import pytest
from aiohttp.test_utils import TestClient, TestServer

from nanobot.desktop.controller import DesktopController
from nanobot.desktop.server import STATE, create_app


@pytest.mark.asyncio
async def test_manager_auth_host_origin_and_bootstrap(tmp_path):
    app = create_app(DesktopController(tmp_path))
    async with TestClient(TestServer(app, host="127.0.0.1")) as client:
        origin = str(client.make_url("/")).rstrip("/")
        app[STATE].origin = origin
        assert (await client.get("/api/status")).status == 401
        token = app[STATE].bootstrap()
        response = await client.post("/api/session", json={"token": token}, headers={"Origin": origin})
        assert response.status == 200
        session = (await response.json())["token"]
        headers = {"Authorization": f"Bearer {session}", "Origin": origin}
        assert (await client.get("/api/status", headers=headers)).status == 200
        assert (await client.post("/api/session", json={"token": token}, headers={"Origin": origin})).status == 401
        assert (await client.post("/api/start", json={}, headers={**headers, "Origin": "https://evil.example"})).status == 403
        assert (await client.get("/api/status", headers={**headers, "Host": "evil.example"})).status == 403
        assert (await client.post("/api/not-a-command", json={}, headers=headers)).status == 404
