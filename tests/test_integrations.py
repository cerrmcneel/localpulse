"""Tests for Google Fit, Android Health Connect, and mobile health webhook integrations."""
from datetime import timedelta
import pytest

from app import config


def test_integration_ingest_single_and_stats(client, second_profile):
    """Can ingest daily activity via webhook and retrieve via daily stats."""
    prof_id = second_profile["id"]
    headers = {"X-Profile-ID": str(prof_id)}

    # 1. Fetch integration config
    cfg_res = client.get("/api/integrations/config", headers=headers)
    assert cfg_res.status_code == 200
    cfg = cfg_res.json()
    assert "api_key" in cfg
    assert cfg["api_key"].startswith("lp_")
    api_key = cfg["api_key"]

    # 2. Ingest health data with API key header
    today_str = config.now().date().isoformat()
    auth_headers = {"X-API-Key": api_key}
    payload = {
        "day": today_str,
        "steps": 9450,
        "active_calories": 420.5,
        "distance_m": 6800.0,
        "heart_rate_avg": 68,
        "sleep_minutes": 460,
        "source": "health_connect",
    }
    ingest_res = client.post("/api/integrations/health", json=payload, headers=auth_headers)
    assert ingest_res.status_code == 200
    res_data = ingest_res.json()
    assert res_data["status"] == "ok"
    assert res_data["latest"]["steps"] == 9450
    assert res_data["latest"]["active_calories"] == 420.5

    # 3. Verify data appears in GET /api/stats/daily
    stats_res = client.get(f"/api/stats/daily?day={today_str}", headers=headers)
    assert stats_res.status_code == 200
    stats = stats_res.json()
    assert "activity" in stats
    act = stats["activity"]
    assert act["steps"] == 9450
    assert act["active_calories"] == 420.5
    assert act["distance_m"] == 6800.0
    assert act["source"] == "health_connect"


def test_integration_ingest_batch_and_weight_sync(client, second_profile):
    """Batch records with alternative keys (e.g. step_count, calories_burned, weight_kg) update cleanly."""
    prof_id = second_profile["id"]
    headers = {"X-Profile-ID": str(prof_id)}
    cfg = client.get("/api/integrations/config", headers=headers).json()
    api_key = cfg["api_key"]

    today = config.now().date()
    yesterday = (today - timedelta(days=1)).isoformat()
    today_str = today.isoformat()

    batch = [
        {
            "day": yesterday,
            "step_count": 8200,
            "calories_burned": 380.0,
            "source": "google_fit",
        },
        {
            "day": today_str,
            "steps": 10500,
            "active_energy_kcal": 510.0,
            "weight_kg": 76.4,
            "source": "google_fit",
        },
    ]

    res = client.post("/api/integrations/google-fit", json=batch, headers={"X-API-Key": api_key})
    assert res.status_code == 200
    assert res.json()["processed_count"] == 2

    # Verify weight was automatically logged
    weight_res = client.get("/api/weights", headers=headers)
    assert weight_res.status_code == 200
    weights = weight_res.json()["weights"]
    matching = [w for w in weights if w["day"] == today_str]
    assert len(matching) == 1
    assert matching[0]["weight_kg"] == 76.4


def test_integration_auth_rejection(client):
    """Requests with missing or invalid API keys are rejected with 401 when auth is required."""
    res = client.post("/api/integrations/health", json={"steps": 5000}, headers={"X-API-Key": "lp_invalid_key_12345"})
    assert res.status_code == 401


def test_integration_regenerate_key(client, second_profile):
    """Can regenerate an API key."""
    headers = {"X-Profile-ID": str(second_profile["id"])}
    before = client.get("/api/integrations/config", headers=headers).json()["api_key"]
    res = client.post("/api/integrations/regenerate-key", headers=headers)
    assert res.status_code == 200
    after = res.json()["api_key"]
    assert before != after
    assert after.startswith("lp_")


def test_integration_manual_log(client, second_profile):
    """Can manually log steps and burn from the web UI."""
    headers = {"X-Profile-ID": str(second_profile["id"])}
    today_str = config.now().date().isoformat()
    res = client.post("/api/integrations/manual", json={
        "day": today_str,
        "steps": 11200,
        "active_calories": 520.0,
        "distance_m": 8200.0,
    }, headers=headers)
    assert res.status_code == 200

    stats = client.get(f"/api/stats/daily?day={today_str}", headers=headers).json()
    assert stats["activity"]["steps"] == 11200
    assert stats["activity"]["active_calories"] == 520.0
    assert stats["activity"]["source"] == "manual"


def test_coaching_reflects_activity_metrics(client, second_profile):
    """Coaching status calculates and reports activity metrics."""
    headers = {"X-Profile-ID": str(second_profile["id"])}
    cfg = client.get("/api/integrations/config", headers=headers).json()
    api_key = cfg["api_key"]

    today = config.now().date()
    # Log 5 days of activity
    records = []
    for i in range(5):
        d = (today - timedelta(days=i)).isoformat()
        records.append({"day": d, "steps": 10000, "active_calories": 450.0})
    client.post("/api/integrations/health", json=records, headers={"X-API-Key": api_key})

    res = client.get("/api/coaching/status", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["avg_steps_14d"] >= 3000
    assert "Active movement" in data["reasoning"] or "steps/day" in data["reasoning"]
