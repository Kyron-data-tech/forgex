import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.database import init_db

@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    await init_db()

@pytest.mark.asyncio
async def test_unauthorized_host_header_blocked():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
        resp = await client.get("/jobs/new", headers={"Host": "malicious-site.com"})
        assert resp.status_code == 400
        assert "Invalid Host header" in resp.json()["detail"]

@pytest.mark.asyncio
async def test_csrf_protection_enforced():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
        get_resp = await client.get("/jobs/new")
        assert get_resp.status_code == 200
        csrf_token = get_resp.cookies.get("csrftoken")
        assert csrf_token is not None

        # POST without CSRF header should be rejected (403)
        client.cookies.set("csrftoken", csrf_token)
        post_resp = await client.post(
            "/api/jobs",
            json={
                "workflow_name": "worldheroes",
                "content_type": "Hero",
                "subject_name": "Test Person",
                "work_date": "2026-10-12"
            }
        )
        assert post_resp.status_code == 403

        # POST with mismatched CSRF token should be rejected (403)
        post_resp_bad = await client.post(
            "/api/jobs",
            json={
                "workflow_name": "worldheroes",
                "content_type": "Hero",
                "subject_name": "Test Person",
                "work_date": "2026-10-12"
            },
            headers={"X-CSRF-Token": "invalid_token_123"}
        )
        assert post_resp_bad.status_code == 403

@pytest.mark.asyncio
async def test_chat_allowlist_forbids_publishing_and_arbitrary_actions():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
        get_resp = await client.get("/jobs/new")
        csrf_token = get_resp.cookies.get("csrftoken")
        client.cookies.set("csrftoken", csrf_token)

        # Chat assistant attempting to trigger forbidden 'publish' action
        resp = await client.post(
            "/api/chat/action",
            json={"action": "publish", "params": {"job_id": 1}},
            headers={"X-CSRF-Token": csrf_token}
        )
        assert resp.status_code == 403
        assert "forbidden" in resp.json()["detail"].lower()

        # Chat assistant attempting arbitrary shell execution
        resp2 = await client.post(
            "/api/chat/action",
            json={"action": "run_command", "params": {"cmd": "whoami"}},
            headers={"X-CSRF-Token": csrf_token}
        )
        assert resp2.status_code == 403

@pytest.mark.asyncio
async def test_headless_credentials_management():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
        get_resp = await client.get("/settings")
        csrf_token = get_resp.cookies.get("csrftoken")
        client.cookies.set("csrftoken", csrf_token)

        # 1. Invalid email rejected
        resp_bad = await client.post(
            "/api/settings/browser/save-credentials",
            json={"email": "not-an-email", "password": "securepassword123"},
            headers={"X-CSRF-Token": csrf_token}
        )
        assert resp_bad.status_code == 400

        # 2. Short password rejected
        resp_short = await client.post(
            "/api/settings/browser/save-credentials",
            json={"email": "test@example.com", "password": "123"},
            headers={"X-CSRF-Token": csrf_token}
        )
        assert resp_short.status_code == 400

        # 3. Valid credentials saved
        resp_ok = await client.post(
            "/api/settings/browser/save-credentials",
            json={"email": "test@example.com", "password": "securepassword123"},
            headers={"X-CSRF-Token": csrf_token}
        )
        assert resp_ok.status_code == 200
        assert resp_ok.json()["success"] is True

        # 4. Status reflects saved credentials
        status_resp = await client.get("/api/settings/browser/status")
        assert status_resp.status_code == 200
        assert status_resp.json()["has_saved_credentials"] is True

        # 5. Clear credentials
        clear_resp = await client.delete(
            "/api/settings/browser/clear-credentials",
            headers={"X-CSRF-Token": csrf_token}
        )
        assert clear_resp.status_code == 200
        assert clear_resp.json()["success"] is True

        # 6. Status reflects cleared credentials
        status_resp2 = await client.get("/api/settings/browser/status")
        assert status_resp2.status_code == 200
        assert status_resp2.json()["has_saved_credentials"] is False
