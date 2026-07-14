from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from coordinator.main import create_app

TOKEN = "test-farm-token"
HEADERS = {"X-Farm-Token": TOKEN}


@pytest.fixture
def client(tmp_path):
    app = create_app(data_dir=tmp_path, token=TOKEN, lease_seconds=120, sweep_interval=3600)
    with TestClient(app) as test_client:
        yield test_client


def submit_job(client, *, start=1, end=3, version="4.5") -> str:
    params = {
        "name": "smoke test",
        "frame_start": start,
        "frame_end": end,
        "frame_step": 1,
        "engine": "CYCLES",
        "output_format": "PNG",
        "blender_version": version,
    }
    response = client.post(
        "/jobs",
        headers=HEADERS,
        files={
            "blend_file": (
                "scene.blend",
                io.BytesIO(b"BLENDER-v1"),
                "application/octet-stream",
            )
        },
        data={"params": json.dumps(params)},
    )
    assert response.status_code == 201, response.text
    return response.json()["job_id"]


def claim(client, worker="worker-01", version="4.5"):
    return client.get(
        "/work",
        headers=HEADERS,
        params={"worker_id": worker, "blender_version": version},
    )


def test_authentication_is_required(client):
    response = client.get("/jobs")
    assert response.status_code == 401


def test_submit_streams_blend_and_creates_frame_rows(client):
    job_id = submit_job(client, start=3, end=7)

    status = client.get(f"/jobs/{job_id}", headers=HEADERS)
    assert status.status_code == 200
    body = status.json()
    assert body["counts"] == {"pending": 5, "rendering": 0, "done": 0, "failed": 0}
    assert [frame["frame"] for frame in body["frames"]] == [3, 4, 5, 6, 7]

    blend = client.get(f"/jobs/{job_id}/blend", headers=HEADERS)
    assert blend.content == b"BLENDER-v1"
    assert len(blend.headers["X-Blend-SHA256"]) == 64


def test_rejects_ranges_above_ten_thousand_frames(client):
    params = {
        "name": "too large",
        "frame_start": 1,
        "frame_end": 10_001,
        "frame_step": 1,
        "engine": "CYCLES",
        "output_format": "PNG",
        "blender_version": "4.5",
    }
    response = client.post(
        "/jobs",
        headers=HEADERS,
        files={"blend_file": ("scene.blend", b"x")},
        data={"params": json.dumps(params)},
    )
    assert response.status_code == 422


def test_claims_each_frame_once_and_rejects_version_mismatch(client):
    job_id = submit_job(client, start=1, end=2, version="4.5.3")

    mismatch = claim(client, "old-worker", "4.4.9")
    assert mismatch.status_code == 204

    first = claim(client, "worker-a", "4.5.0")
    second = claim(client, "worker-b", "4.5.2")
    empty = claim(client, "worker-c", "4.5")

    assert (first.status_code, second.status_code, empty.status_code) == (200, 200, 204)
    assert {first.json()["frame"], second.json()["frame"]} == {1, 2}
    assert first.json()["job_id"] == job_id
    assert first.json()["blend_url"] == f"/jobs/{job_id}/blend"


def test_result_must_come_from_current_lease_holder(client):
    job_id = submit_job(client, start=1, end=1)
    assignment = claim(client, "worker-a").json()
    assert assignment["frame"] == 1

    late = client.post(
        f"/jobs/{job_id}/frames/1/result",
        headers=HEADERS,
        data={"worker_id": "worker-b", "render_seconds": "3.2"},
        files={"frame_file": ("frame_0001.png", b"png")},
    )
    assert late.status_code == 409, late.text


def test_result_after_lease_deadline_is_rejected(client):
    job_id = submit_job(client, start=1, end=1)
    claim(client, "worker-a")
    expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    client.app.state.db.set_lease_for_test(job_id, 1, expired)

    late = client.post(
        f"/jobs/{job_id}/frames/1/result",
        headers=HEADERS,
        data={"worker_id": "worker-a", "render_seconds": "3.2"},
        files={"frame_file": ("frame_0001.png", b"png")},
    )

    assert late.status_code == 409, late.text


def test_successful_result_completes_job_and_is_downloadable(client):
    job_id = submit_job(client, start=1, end=1)
    claim(client, "worker-a")

    result = client.post(
        f"/jobs/{job_id}/frames/1/result",
        headers=HEADERS,
        data={"worker_id": "worker-a", "render_seconds": "2.5"},
        files={"frame_file": ("frame_0001.png", b"valid-png-bytes")},
    )
    assert result.status_code == 200, result.text
    assert client.get(f"/jobs/{job_id}", headers=HEADERS).json()["status"] == "complete"

    archive = client.get(f"/jobs/{job_id}/frames.zip", headers=HEADERS)
    assert archive.status_code == 200
    with zipfile.ZipFile(io.BytesIO(archive.content)) as zipped:
        assert zipped.namelist() == ["frame_0001.png"]
        assert zipped.read("frame_0001.png") == b"valid-png-bytes"


def test_failures_requeue_until_third_attempt_and_can_be_manually_requeued(client):
    job_id = submit_job(client, start=1, end=1)

    for attempt in range(1, 4):
        assert claim(client, "worker-a").status_code == 200
        failure = client.post(
            f"/jobs/{job_id}/frames/1/result",
            headers={**HEADERS, "content-type": "application/json"},
            content=json.dumps(
                {
                    "status": "failed",
                    "worker_id": "worker-a",
                    "exit_code": 1,
                    "stderr_tail": f"GPU error {attempt}",
                }
            ),
        )
        assert failure.status_code == 200

    status = client.get(f"/jobs/{job_id}", headers=HEADERS).json()
    assert status["counts"]["failed"] == 1
    assert status["frames"][0]["stderr_tail"] == "GPU error 3"

    requeue = client.post(f"/jobs/{job_id}/frames/1/requeue", headers=HEADERS)
    assert requeue.status_code == 200
    frame = client.get(f"/jobs/{job_id}", headers=HEADERS).json()["frames"][0]
    assert (frame["state"], frame["attempts"], frame["stderr_tail"]) == ("pending", 0, None)


def test_cancel_stops_pending_assignments(client):
    job_id = submit_job(client)
    response = client.post(f"/jobs/{job_id}/cancel", headers=HEADERS)
    assert response.status_code == 200
    assert claim(client).status_code == 204


def test_expired_lease_is_requeued(client):
    job_id = submit_job(client, start=1, end=1)
    claim(client, "dead-worker")
    expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    client.app.state.db.set_lease_for_test(job_id, 1, expired)

    changed = client.app.state.db.sweep_expired_leases(datetime.now(UTC))

    assert changed == 1
    frame = client.get(f"/jobs/{job_id}", headers=HEADERS).json()["frames"][0]
    assert (frame["state"], frame["attempts"]) == ("pending", 1)


def test_dashboard_is_public_but_api_remains_protected(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "Farmhand" in page.text
    assert client.get("/favicon.ico").status_code == 204
    assert client.get("/jobs").status_code == 401
