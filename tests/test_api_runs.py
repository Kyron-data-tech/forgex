"""
Unit & Integration Tests for API Runs & Subprocess Worker Controls
"""

import pytest
import asyncio
from httpx import AsyncClient, ASGITransport
from pathlib import Path

from app.main import app
from app.config import settings


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.asyncio
async def test_create_run_and_controls():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
        # Get CSRF cookie first
        init_res = await client.get("/jobs/new")
        csrf_token = client.cookies.get("csrftoken", "")

        # Create Run
        headers = {"x-csrf-token": csrf_token} if csrf_token else {}
        res = await client.post(
            "/api/runs",
            json={
                "task": "Test Operator Run",
                "subject_name": "Katherine Johnson",
                "work_date": "1969",
            },
            headers=headers,
        )

        assert res.status_code == 201
        data = res.json()
        assert "run_id" in data
        run_id = data["run_id"]

        # Check run details
        get_res = await client.get(f"/api/runs/{run_id}")
        assert get_res.status_code == 200
        run_details = get_res.json()
        assert run_details["subject_name"] == "Katherine Johnson"
        assert run_details["work_date"] == "1969"

        # Test pause command
        pause_res = await client.post(f"/api/runs/{run_id}/pause", headers=headers)
        assert pause_res.status_code == 200

        # Test resume command
        resume_res = await client.post(f"/api/runs/{run_id}/resume", headers=headers)
        assert resume_res.status_code == 200

        # Test stop command
        stop_res = await client.post(f"/api/runs/{run_id}/stop", headers=headers)
        assert stop_res.status_code == 200

        # Verify status is STOPPED
        stopped_details = (await client.get(f"/api/runs/{run_id}")).json()
        assert stopped_details["status"] == "STOPPED"


@pytest.mark.asyncio
async def test_batch_runs_creation():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:

        init_res = await client.get("/jobs/new")
        csrf_token = client.cookies.get("csrftoken", "")
        headers = {"x-csrf-token": csrf_token} if csrf_token else {}

        # Create temporary batch CSV in data_storage/inputs
        inputs_dir = settings.DEFAULT_DATA_DIR / "inputs"
        inputs_dir.mkdir(parents=True, exist_ok=True)
        batch_file = inputs_dir / "api_batch_test.csv"
        batch_file.write_text("Hero,Year\nAda Lovelace,1843\nAlan Turing,1936\n", encoding="utf-8")

        res = await client.post(
            "/api/runs/batch",
            json={
                "file_path": str(batch_file),
                "column_mapping": {"subject_name": "Hero", "work_date": "Year"},
            },
            headers=headers,
        )

        assert res.status_code == 200
        data = res.json()
        assert data["total_rows"] == 2
        assert data["valid_count"] == 2
        assert len(data["created_runs"]) == 2


@pytest.mark.asyncio
async def test_date_only_run_and_synthesize():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
        init_res = await client.get("/jobs/new")
        csrf_token = client.cookies.get("csrftoken", "")
        headers = {"x-csrf-token": csrf_token} if csrf_token else {}

        # 1. Create run with date only
        res = await client.post(
            "/api/runs",
            json={
                "work_date": "October 14",
            },
            headers=headers,
        )
        assert res.status_code == 201
        data = res.json()
        run_id = data["run_id"]

        # Check run details
        details = (await client.get(f"/api/runs/{run_id}")).json()
        assert details["work_date"] == "October 14"
        assert "October 14" in details["task"]

        # 2. Test synthesize command
        syn_res = await client.post(f"/api/runs/{run_id}/synthesize", headers=headers)
        assert syn_res.status_code == 200

        # Stop run
        await client.post(f"/api/runs/{run_id}/stop", headers=headers)

