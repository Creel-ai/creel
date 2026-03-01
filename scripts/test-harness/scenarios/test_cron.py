"""HARNESS-006: Cron job lifecycle integration tests.

Tests CRUD operations, manual triggering, run history, and enable/disable
for cron jobs via the daemon API.
"""

from __future__ import annotations

import httpx
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def create_job(
    client: httpx.Client,
    name: str = "test-job",
    message: str = "hello",
    schedule_kind: str = "cron",
    schedule_expr: str = "0 0 * * *",
    enabled: bool = True,
) -> httpx.Response:
    """POST /v1/cron/jobs and return the raw response."""
    return client.post(
        "/v1/cron/jobs",
        json={
            "name": name,
            "schedule": {"kind": schedule_kind, "expr": schedule_expr, "tz": "UTC"},
            "payload": {"kind": "agentTurn", "message": message, "timeout_seconds": 30},
            "delivery": {"mode": "none"},
            "enabled": enabled,
        },
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCreateCronJob:
    """POST /v1/cron/jobs creates a new cron job."""

    def test_create_job_returns_201(self, daemon_client: httpx.Client):
        resp = create_job(daemon_client, name="create-test")
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "create-test"
        assert data["id"]
        assert data["enabled"] is True
        assert data["schedule"]["kind"] == "cron"

    def test_created_job_appears_in_list(self, daemon_client: httpx.Client):
        resp = create_job(daemon_client, name="list-visible")
        assert resp.status_code == 201
        job_id = resp.json()["id"]

        list_resp = daemon_client.get("/v1/cron/jobs")
        assert list_resp.status_code == 200
        jobs = list_resp.json()
        ids = [j["id"] for j in jobs]
        assert job_id in ids


class TestListAndGetCronJobs:
    """GET /v1/cron/jobs and GET /v1/cron/jobs/{id}."""

    def test_list_returns_200(self, daemon_client: httpx.Client):
        resp = daemon_client.get("/v1/cron/jobs")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_get_single_job(self, daemon_client: httpx.Client):
        create_resp = create_job(daemon_client, name="get-single")
        job_id = create_resp.json()["id"]

        resp = daemon_client.get(f"/v1/cron/jobs/{job_id}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "get-single"

    def test_get_nonexistent_returns_404(self, daemon_client: httpx.Client):
        resp = daemon_client.get("/v1/cron/jobs/nonexistent999")
        assert resp.status_code == 404


class TestTriggerCronJob:
    """POST /v1/cron/jobs/{id}/run triggers immediate execution with mock LLM."""

    def test_trigger_returns_202(self, daemon_client: httpx.Client):
        create_resp = create_job(daemon_client, name="trigger-test", message="hello")
        assert create_resp.status_code == 201
        job_id = create_resp.json()["id"]

        trigger_resp = daemon_client.post(
            f"/v1/cron/jobs/{job_id}/run",
            timeout=60.0,  # job execution involves LLM call
        )
        assert trigger_resp.status_code == 202
        assert trigger_resp.json()["status"] == "triggered"

    def test_trigger_creates_run_record(
        self, daemon_client: httpx.Client, mock_client: httpx.Client
    ):
        """After triggering, run history shows a success record with timestamps."""
        create_resp = create_job(daemon_client, name="run-record-test", message="hello")
        job_id = create_resp.json()["id"]

        # Trigger execution
        daemon_client.post(f"/v1/cron/jobs/{job_id}/run", timeout=60.0)

        # Check run history
        runs_resp = daemon_client.get(f"/v1/cron/jobs/{job_id}/runs")
        assert runs_resp.status_code == 200
        runs = runs_resp.json()
        assert len(runs) >= 1

        last_run = runs[-1]
        assert last_run["job_id"] == job_id
        assert last_run["started_at"]
        assert last_run["ended_at"]
        assert last_run["status"] == "success"
        assert last_run["error"] is None

    def test_trigger_nonexistent_returns_404(self, daemon_client: httpx.Client):
        resp = daemon_client.post("/v1/cron/jobs/nonexistent999/run")
        assert resp.status_code == 404


class TestUpdateCronJob:
    """PUT /v1/cron/jobs/{id} updates job fields."""

    def test_update_prompt(self, daemon_client: httpx.Client):
        create_resp = create_job(daemon_client, name="update-test", message="original prompt")
        job_id = create_resp.json()["id"]

        update_resp = daemon_client.put(
            f"/v1/cron/jobs/{job_id}",
            json={"payload": {"kind": "agentTurn", "message": "updated prompt"}},
        )
        assert update_resp.status_code == 200
        assert update_resp.json()["payload"]["message"] == "updated prompt"

        # Verify the change persists when fetched
        get_resp = daemon_client.get(f"/v1/cron/jobs/{job_id}")
        assert get_resp.json()["payload"]["message"] == "updated prompt"

    def test_update_name(self, daemon_client: httpx.Client):
        create_resp = create_job(daemon_client, name="name-before")
        job_id = create_resp.json()["id"]

        update_resp = daemon_client.put(
            f"/v1/cron/jobs/{job_id}",
            json={"name": "name-after"},
        )
        assert update_resp.status_code == 200
        assert update_resp.json()["name"] == "name-after"

    def test_update_nonexistent_returns_404(self, daemon_client: httpx.Client):
        resp = daemon_client.put(
            "/v1/cron/jobs/nonexistent999",
            json={"name": "nope"},
        )
        assert resp.status_code == 404


class TestDeleteCronJob:
    """DELETE /v1/cron/jobs/{id} removes a job."""

    def test_delete_removes_from_list(self, daemon_client: httpx.Client):
        create_resp = create_job(daemon_client, name="delete-test")
        job_id = create_resp.json()["id"]

        # Verify it exists
        assert daemon_client.get(f"/v1/cron/jobs/{job_id}").status_code == 200

        # Delete it
        del_resp = daemon_client.delete(f"/v1/cron/jobs/{job_id}")
        assert del_resp.status_code == 200
        assert del_resp.json()["id"] == job_id

        # Verify it's gone
        assert daemon_client.get(f"/v1/cron/jobs/{job_id}").status_code == 404

    def test_delete_nonexistent_returns_404(self, daemon_client: httpx.Client):
        resp = daemon_client.delete("/v1/cron/jobs/nonexistent999")
        assert resp.status_code == 404


class TestDisableEnableCronJob:
    """POST /v1/cron/jobs/{id}/disable and /enable toggle scheduling."""

    def test_disable_sets_enabled_false(self, daemon_client: httpx.Client):
        create_resp = create_job(daemon_client, name="disable-test", enabled=True)
        job_id = create_resp.json()["id"]

        disable_resp = daemon_client.post(f"/v1/cron/jobs/{job_id}/disable")
        assert disable_resp.status_code == 200
        assert disable_resp.json()["enabled"] is False

    def test_enable_sets_enabled_true(self, daemon_client: httpx.Client):
        create_resp = create_job(daemon_client, name="enable-test", enabled=False)
        job_id = create_resp.json()["id"]

        enable_resp = daemon_client.post(f"/v1/cron/jobs/{job_id}/enable")
        assert enable_resp.status_code == 200
        assert enable_resp.json()["enabled"] is True

    def test_disabled_job_trigger_still_runs(self, daemon_client: httpx.Client):
        """Disabled jobs can still be manually triggered (trigger bypasses scheduler)."""
        create_resp = create_job(daemon_client, name="disabled-trigger", message="hello")
        job_id = create_resp.json()["id"]

        # Disable the job
        daemon_client.post(f"/v1/cron/jobs/{job_id}/disable")

        # Manual trigger should still work
        trigger_resp = daemon_client.post(
            f"/v1/cron/jobs/{job_id}/run", timeout=60.0
        )
        assert trigger_resp.status_code == 202

        # Run history should have a record
        runs_resp = daemon_client.get(f"/v1/cron/jobs/{job_id}/runs")
        runs = runs_resp.json()
        assert len(runs) >= 1


class TestUpdateAndRetrigger:
    """Update a job's prompt, re-trigger, and verify the new prompt is used."""

    def test_updated_prompt_used_on_retrigger(
        self, daemon_client: httpx.Client, mock_client: httpx.Client
    ):
        """After updating the payload message, a re-trigger uses the new prompt."""
        create_resp = create_job(
            daemon_client, name="retrigger-test", message="first prompt"
        )
        job_id = create_resp.json()["id"]

        # Trigger with original prompt
        daemon_client.post(f"/v1/cron/jobs/{job_id}/run", timeout=60.0)

        # Update the prompt
        daemon_client.put(
            f"/v1/cron/jobs/{job_id}",
            json={"payload": {"kind": "agentTurn", "message": "second prompt"}},
        )

        # Reset mock history to isolate the second trigger
        mock_client.post("/v1/mock/reset")

        # Re-trigger with updated prompt
        daemon_client.post(f"/v1/cron/jobs/{job_id}/run", timeout=60.0)

        # Verify the mock LLM received the updated prompt
        history = mock_client.get("/v1/mock/history").json()
        assert len(history["calls"]) >= 1
        last_call = history["calls"][-1]
        messages = last_call["body"].get("messages", [])
        all_content = " ".join(str(m.get("content", "")) for m in messages)
        assert "second prompt" in all_content, (
            f"Updated prompt not found in LLM call: {all_content[:300]}"
        )

        # Run history should now have 2 records
        runs_resp = daemon_client.get(f"/v1/cron/jobs/{job_id}/runs")
        runs = runs_resp.json()
        assert len(runs) >= 2
