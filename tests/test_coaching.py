"""Tests for adaptive TDEE coaching and weekly check-in system."""
from datetime import timedelta
import pytest
from app import config


def test_coaching_status_initial_cold_start(client, second_profile):
    """When a new profile has little data, coaching uses baseline Mifflin-St Jeor TDEE."""
    headers = {"X-Profile-ID": str(second_profile["id"])}
    res = client.get("/api/coaching/status", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert "estimated_tdee" in data
    assert data["estimated_tdee"] > 1200
    assert data["goal"] in ("cut", "maintain", "bulk")
    assert "current_targets" in data
    assert "recommended_targets" in data


def test_coaching_adaptive_tdee_calculation(client, second_profile):
    """With logged nutrition and weights, adaptive TDEE reflects energy balance."""
    headers = {"X-Profile-ID": str(second_profile["id"])}
    today = config.now().date()

    # Log 10 days of food and weigh-ins over the last 12 days
    # User weighs 80 kg and loses 0.5 kg over 10 days while eating 2,000 kcal/day
    for i in range(10):
        day_date = today - timedelta(days=9 - i)
        day_str = day_date.isoformat()
        # Weight drops from 80.5 kg down to 80.0 kg (-0.5 kg in 9 days ~ -0.39 kg/wk)
        weight = 80.5 - (0.5 * (i / 9.0))
        client.post("/api/weights", json={"weight_kg": round(weight, 2), "day": day_str}, headers=headers)

        # Log 2000 kcal meal
        client.post("/api/meals", json={
            "name": f"Day {i} Meal",
            "day": day_str,
            "items": [{"name": "Chicken rice bowl", "calories": 2000, "protein_g": 160, "carbs_g": 200, "fat_g": 60}],
        }, headers=headers)

    res = client.get("/api/coaching/status", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["confidence"] in ("high", "medium")
    assert data["days_logged_14d"] >= 4
    assert data["weigh_ins_14d"] >= 3
    # Weight trend dropped, so estimated TDEE should be higher than intake (2000 kcal)
    assert data["estimated_tdee"] > 2000


def test_apply_weekly_checkin(client, second_profile):
    """Applying a check-in updates the profile's calorie and macro targets."""
    headers = {"X-Profile-ID": str(second_profile["id"])}
    status_before = client.get("/api/coaching/status", headers=headers).json()
    rec_cals = status_before["recommended_targets"]["calories"]

    res = client.post("/api/coaching/checkin", json={"action": "apply"}, headers=headers)
    assert res.status_code == 200
    res_data = res.json()
    assert res_data["status"] == "applied"
    assert res_data["new_targets"]["calories"] == rec_cals

    # Profile targets are updated
    prof = client.get(f"/api/profiles/{second_profile['id']}", headers=headers).json()
    assert prof["calorie_target"] == rec_cals

    # Status is now up to date
    status_after = client.get("/api/coaching/status", headers=headers).json()
    assert status_after["status"] == "up_to_date"
    assert status_after["days_since_checkin"] == 0

    # Recorded in history
    hist = client.get("/api/coaching/history", headers=headers).json()
    assert len(hist["checkins"]) >= 1
    assert hist["checkins"][0]["status"] == "applied"


def test_skip_checkin(client, second_profile):
    """Skipping a check-in records the checkin event but keeps targets intact."""
    headers = {"X-Profile-ID": str(second_profile["id"])}
    prof_before = client.get(f"/api/profiles/{second_profile['id']}", headers=headers).json()
    cals_before = prof_before["calorie_target"]

    res = client.post("/api/coaching/checkin", json={"action": "skip"}, headers=headers)
    assert res.status_code == 200
    assert res.json()["status"] == "skipped"

    # Targets unchanged
    prof_after = client.get(f"/api/profiles/{second_profile['id']}", headers=headers).json()
    assert prof_after["calorie_target"] == cals_before


def test_update_coaching_settings(client, second_profile):
    """Can change goal between cut, maintain, bulk, and toggle pause."""
    headers = {"X-Profile-ID": str(second_profile["id"])}

    # Switch to maintain
    res = client.patch("/api/coaching/settings", json={"goal": "maintain"}, headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["goal"] == "maintain"
    assert data["goal_rate_kg_per_week"] == 0.0

    # Pause coaching
    res_pause = client.patch("/api/coaching/settings", json={"coaching_paused": True}, headers=headers)
    assert res_pause.status_code == 200
    assert res_pause.json()["status"] == "paused"
