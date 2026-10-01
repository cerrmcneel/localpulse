"""Daily totals, meal-slot inference, and model-output normalisation."""
from datetime import datetime, timedelta

import pytest

from app import config
from app.routers.meals import infer_meal_type
from app.services import vision


# --- meal slot inference (Spanish eating schedule) ---

@pytest.mark.parametrize("hour,minute,expected", [
    (7, 0, "breakfast"),
    (12, 0, "breakfast"),
    (12, 30, "lunch"),
    (14, 30, "lunch"),      # la comida
    (16, 0, "snack"),       # la merienda
    (19, 59, "snack"),
    (20, 0, "dinner"),      # la cena
    (22, 30, "dinner"),
    (2, 0, "snack"),        # late night
])
def test_meal_slot_matches_local_eating_schedule(hour, minute, expected):
    when = datetime(2026, 9, 10, hour, minute, tzinfo=config.TZ)
    assert infer_meal_type(when) == expected


# --- model output normalisation ---

def test_calories_are_recomputed_when_they_contradict_the_macros():
    data = {"dish": "x", "items": [
        {"name": "rice", "grams": 200, "calories": 10,
         "protein_g": 5, "carbs_g": 50, "fat_g": 1, "confidence": "medium"},
    ]}
    out = vision.normalize(data, model="test")
    # 5*4 + 50*4 + 1*9 = 229, not the 10 the model claimed.
    assert out["items"][0]["calories"] == pytest.approx(229.0)


def test_negative_and_nonsense_values_are_clamped():
    data = {"dish": "x", "items": [
        {"name": "oil", "grams": -50, "calories": "abc",
         "protein_g": None, "carbs_g": 0, "fat_g": 10, "confidence": "bogus"},
    ]}
    out = vision.normalize(data, model="test")
    item = out["items"][0]
    assert item["grams"] == 0
    assert item["protein_g"] == 0
    assert item["confidence"] == "medium"
    assert item["calories"] == pytest.approx(90.0)


def test_items_with_no_nutrition_at_all_are_dropped():
    data = {"dish": "x", "items": [
        {"name": "parsley garnish", "grams": 1, "calories": 0,
         "protein_g": 0, "carbs_g": 0, "fat_g": 0, "confidence": "low"},
        {"name": "steak", "grams": 200, "calories": 0,
         "protein_g": 50, "carbs_g": 0, "fat_g": 20, "confidence": "high"},
    ]}
    out = vision.normalize(data, model="test")
    assert [i["name"] for i in out["items"]] == ["steak"]


def test_totals_sum_every_item():
    items = [
        {"calories": 100, "protein_g": 10, "carbs_g": 5, "fat_g": 2, "grams": 50},
        {"calories": 200, "protein_g": 20, "carbs_g": 10, "fat_g": 4, "grams": 100},
    ]
    totals = vision.totals_for(items)
    assert totals["calories"] == 300
    assert totals["protein_g"] == 30
    assert totals["grams"] == 150


# --- daily totals ---

def test_daily_totals_reflect_logged_meals(client, second_profile):
    headers = {"X-Profile-ID": str(second_profile["id"])}
    client.post("/api/meals", json={
        "name": "Breakfast",
        "items": [
            {"name": "eggs", "calories": 200, "protein_g": 18, "carbs_g": 2, "fat_g": 14},
            {"name": "toast", "calories": 150, "protein_g": 5, "carbs_g": 28, "fat_g": 2},
        ],
    }, headers=headers)

    stats = client.get("/api/stats/daily", headers=headers).json()
    assert stats["totals"]["calories"] == pytest.approx(350.0)
    assert stats["totals"]["protein_g"] == pytest.approx(23.0)
    assert stats["totals"]["meal_count"] == 1


def test_editing_a_meal_moves_the_daily_total(client, second_profile):
    """Totals are a view over meal_items, so they can never drift out of sync."""
    headers = {"X-Profile-ID": str(second_profile["id"])}
    meal = client.post("/api/meals", json={
        "name": "Lunch", "items": [{"name": "pasta", "calories": 600}],
    }, headers=headers).json()

    before = client.get("/api/stats/daily", headers=headers).json()["totals"]["calories"]

    client.patch(f"/api/meals/{meal['id']}",
                 json={"items": [{"name": "pasta", "calories": 300}]},
                 headers=headers)

    after = client.get("/api/stats/daily", headers=headers).json()["totals"]["calories"]
    assert after == pytest.approx(before - 300)


def test_range_series_is_dense_including_unlogged_days(client, second_profile):
    headers = {"X-Profile-ID": str(second_profile["id"])}
    data = client.get("/api/stats/range?days=14", headers=headers).json()
    assert len(data["series"]) == 14
    days = [s["day"] for s in data["series"]]
    assert days == sorted(days), "series must be chronological"
    assert days[-1] == config.now().date().isoformat()


def test_log_meal_for_past_day(client, second_profile):
    """User can log meals for yesterday or any day in the past."""
    headers = {"X-Profile-ID": str(second_profile["id"])}
    yesterday = (config.now().date() - timedelta(days=1)).isoformat()
    today = config.now().date().isoformat()

    res = client.post("/api/meals", json={
        "name": "Late Dinner",
        "day": yesterday,
        "meal_type": "dinner",
        "items": [
            {"name": "Salmon salad", "calories": 450, "protein_g": 35, "carbs_g": 12, "fat_g": 20},
        ],
    }, headers=headers)
    assert res.status_code == 201
    meal = res.json()
    assert meal["day"] == yesterday
    assert meal["name"] == "Late Dinner"

    # Meal is visible in yesterday's meals list
    yest_meals = client.get(f"/api/meals?day={yesterday}", headers=headers).json()
    assert any(m["id"] == meal["id"] for m in yest_meals["meals"])

    # Meal is NOT in today's meals list
    today_meals = client.get(f"/api/meals?day={today}", headers=headers).json()
    assert all(m["id"] != meal["id"] for m in today_meals["meals"])

    # Yesterday's daily stats reflect the meal
    yest_stats = client.get(f"/api/stats/daily?day={yesterday}", headers=headers).json()
    assert yest_stats["totals"]["calories"] == pytest.approx(450.0)
    assert yest_stats["totals"]["protein_g"] == pytest.approx(35.0)
    assert yest_stats["totals"]["meal_count"] == 1


def test_duplicate_meal_to_target_past_day(client, second_profile):
    """Duplicate a meal into a specific past day."""
    headers = {"X-Profile-ID": str(second_profile["id"])}
    yesterday = (config.now().date() - timedelta(days=1)).isoformat()

    # Create original meal today
    src = client.post("/api/meals", json={
        "name": "Oatmeal",
        "items": [{"name": "Oats", "calories": 300, "protein_g": 10, "carbs_g": 50, "fat_g": 5}],
    }, headers=headers).json()

    # Duplicate to yesterday
    dup_res = client.post(f"/api/meals/{src['id']}/duplicate?day={yesterday}", json={}, headers=headers)
    assert dup_res.status_code == 201
    dup_meal = dup_res.json()
    assert dup_meal["day"] == yesterday

    yest_meals = client.get(f"/api/meals?day={yesterday}", headers=headers).json()
    assert any(m["id"] == dup_meal["id"] for m in yest_meals["meals"])


def test_move_meal_to_past_day_via_patch(client, second_profile):
    """User can edit an existing meal to move it to yesterday."""
    headers = {"X-Profile-ID": str(second_profile["id"])}
    yesterday = (config.now().date() - timedelta(days=1)).isoformat()
    today = config.now().date().isoformat()

    meal = client.post("/api/meals", json={
        "name": "Midnight Snack",
        "items": [{"name": "Greek yogurt", "calories": 150, "protein_g": 15, "carbs_g": 6, "fat_g": 0}],
    }, headers=headers).json()

    # Move to yesterday
    patch_res = client.patch(f"/api/meals/{meal['id']}", json={"day": yesterday}, headers=headers)
    assert patch_res.status_code == 200
    assert patch_res.json()["day"] == yesterday

    # Verify moved out of today and into yesterday
    today_meals = client.get(f"/api/meals?day={today}", headers=headers).json()
    assert all(m["id"] != meal["id"] for m in today_meals["meals"])

    yest_meals = client.get(f"/api/meals?day={yesterday}", headers=headers).json()
    assert any(m["id"] == meal["id"] for m in yest_meals["meals"])


def test_meal_with_floats_logging_duplicating_and_updating(client, second_profile):
    """Meals with float macros (e.g. from AI estimates) can be logged, duplicated, and patched."""
    headers = {"X-Profile-ID": str(second_profile["id"])}
    today = config.now().date().isoformat()

    # 1. Create meal with float macros
    res = client.post("/api/meals", json={
        "name": "AI Protein Bowl",
        "meal_type": "lunch",
        "items": [
            {"name": "Chicken & Rice", "grams": 175.5, "calories": 345.8, "protein_g": 31.4, "carbs_g": 35.6, "fat_g": 7.2},
        ],
    }, headers=headers)
    assert res.status_code == 201
    meal = res.json()
    item = meal["items"][0]
    assert item["grams"] == pytest.approx(175.5)
    assert item["calories"] == pytest.approx(345.8)
    assert item["protein_g"] == pytest.approx(31.4)
    assert item["carbs_g"] == pytest.approx(35.6)
    assert item["fat_g"] == pytest.approx(7.2)

    # 2. Duplicate meal with floats
    dup_res = client.post(f"/api/meals/{meal['id']}/duplicate", json={}, headers=headers)
    assert dup_res.status_code == 201
    dup_meal = dup_res.json()
    dup_item = dup_meal["items"][0]
    assert dup_item["protein_g"] == pytest.approx(31.4)
    assert dup_item["calories"] == pytest.approx(345.8)

    # 3. Update (edit) duplicated meal with user-adjusted float macros
    patch_res = client.patch(f"/api/meals/{dup_meal['id']}", json={
        "items": [
            {"name": "Chicken & Rice (larger)", "grams": 220.5, "calories": 435.5, "protein_g": 39.5, "carbs_g": 44.8, "fat_g": 9.1},
        ],
    }, headers=headers)
    assert patch_res.status_code == 200
    patched_meal = patch_res.json()
    patched_item = patched_meal["items"][0]
    assert patched_item["grams"] == pytest.approx(220.5)
    assert patched_item["calories"] == pytest.approx(435.5)
    assert patched_item["protein_g"] == pytest.approx(39.5)


def test_open_image_supports_jpeg_and_heif():
    """Images service accepts standard JPEGs and phone HEIC/HEIF photos."""
    import io
    from PIL import Image
    import pillow_heif
    from app.services.images import open_image, ImageError

    # JPEG
    buf_jpeg = io.BytesIO()
    Image.new("RGB", (80, 80), color="blue").save(buf_jpeg, format="JPEG")
    img_from_jpeg = open_image(buf_jpeg.getvalue())
    assert img_from_jpeg.size == (80, 80)
    assert img_from_jpeg.mode == "RGB"

    # HEIF / HEIC
    buf_heif = io.BytesIO()
    pillow_heif.from_pillow(Image.new("RGB", (70, 70), color="red")).save(buf_heif)
    img_from_heif = open_image(buf_heif.getvalue())
    assert img_from_heif.size == (70, 70)
    assert img_from_heif.mode == "RGB"

    # Corrupted / invalid bytes
    with pytest.raises(ImageError, match="Unsupported or unreadable"):
        open_image(b"not an image at all")




# --- workouts: day filtering and the local-time week window ---

def test_workout_list_can_be_filtered_to_a_single_day(client, second_profile):
    """The dashboard requests ?day=... ; ignoring it shows stale workouts as today's."""
    headers = {"X-Profile-ID": str(second_profile["id"])}
    yesterday = (config.now().date() - timedelta(days=1)).isoformat()

    client.post("/api/workouts", json={
        "title": "Yesterday session", "duration_min": 30, "day": yesterday,
    }, headers=headers)

    today_only = client.get(
        f"/api/workouts?day={config.now().date().isoformat()}", headers=headers
    ).json()
    assert all(w["day"] != yesterday for w in today_only["workouts"]), (
        "?day= was ignored, so the dashboard will label an old workout as today's"
    )


def test_this_week_window_covers_exactly_seven_days(client, second_profile):
    """`date('now','-7 days')` spans eight days: today plus seven prior.

    Deliberately clock-independent. The related defect -- that SQLite's
    `date('now')` is UTC while the app's day boundary is `config.TZ` -- only
    manifests between local midnight and the UTC offset, so it cannot be pinned
    without freezing time; fixing this off-by-one by computing the cutoff from
    `config.now()` resolves both at once.
    """
    headers = {"X-Profile-ID": str(second_profile["id"])}

    # One 10-minute workout on each of the last nine days, including today.
    for offset in range(9):
        client.post("/api/workouts", json={
            "title": f"Day -{offset}",
            "duration_min": 10,
            "day": (config.now().date() - timedelta(days=offset)).isoformat(),
        }, headers=headers)

    week = client.get("/api/workouts", headers=headers).json()["this_week"]
    assert week["sessions"] == 7, (
        f"'this week' counted {week['sessions']} sessions; a 7-day window ending "
        f"today must count exactly 7"
    )
    assert week["total_minutes"] == 70
