"""Adaptive Macro Coaching & TDEE Calculation Engine.

Calculates real metabolic expenditure (TDEE) using the energy balance equation:
    Energy Expended = Energy Consumed - Energy Stored/Lost (from scale weight trend)

Enforces weekly coaching cadences, safe adjustment damping (±100-150 kcal max shift),
confidence gating, and ISSN clinical macro distribution.
"""
from datetime import date, datetime, timedelta
import math
from typing import Any

from app import config
from app.db import get_conn
from app.deps import get_profile
from app.services.targets import compute_bmr, compute_targets, compute_tdee, KCAL_PER_KG_FAT


def _linear_regression_slope(points: list[tuple[float, float]]) -> float:
    """Least-squares slope m for points (x, y). Returns 0.0 if points < 2."""
    n = len(points)
    if n < 2:
        return 0.0
    sum_x = sum(p[0] for p in points)
    sum_y = sum(p[1] for p in points)
    sum_xy = sum(p[0] * p[1] for p in points)
    sum_xx = sum(p[0] ** 2 for p in points)
    denom = (n * sum_xx) - (sum_x ** 2)
    if abs(denom) < 1e-9:
        return 0.0
    return ((n * sum_xy) - (sum_x * sum_y)) / denom


def calculate_adaptive_coaching(conn, profile_id: int, as_of: date | None = None) -> dict[str, Any]:
    """Evaluate metabolic rate and compute next weekly target recommendation."""
    prof = get_profile(conn, profile_id)
    as_of_date = as_of or config.now().date()
    as_of_iso = as_of_date.isoformat()

    start_14d = (as_of_date - timedelta(days=13)).isoformat()
    start_7d = (as_of_date - timedelta(days=6)).isoformat()
    start_21d = (as_of_date - timedelta(days=20)).isoformat()

    # 1. Fetch weight logs over the last 21 days
    weight_rows = conn.execute(
        """SELECT day, weight_kg FROM weights
           WHERE profile_id = ? AND day BETWEEN ? AND ?
           ORDER BY day ASC""",
        (profile_id, start_21d, as_of_iso),
    ).fetchall()

    # 2. Fetch daily nutrition totals over the last 21 days
    intake_rows = {
        r["day"]: dict(r)
        for r in conn.execute(
            """SELECT day, calories, meal_count FROM v_daily_totals
               WHERE profile_id = ? AND day BETWEEN ? AND ?""",
            (profile_id, start_21d, as_of_iso),
        ).fetchall()
    }

    # 2.1 Fetch daily activity data over the last 21 days
    activity_rows = conn.execute(
        """SELECT day, steps, active_calories FROM daily_activities
           WHERE profile_id = ? AND day BETWEEN ? AND ?
           ORDER BY day ASC""",
        (profile_id, start_21d, as_of_iso),
    ).fetchall()

    act_14d = [r for r in activity_rows if r["day"] >= start_14d and (r["steps"] > 0 or r["active_calories"] > 0)]
    avg_steps_14d = round(sum(r["steps"] for r in act_14d) / len(act_14d)) if act_14d else 0
    avg_active_burn_14d = round(sum(r["active_calories"] for r in act_14d) / len(act_14d), 1) if act_14d else 0.0

    # Count data density
    weigh_ins_7d = sum(1 for r in weight_rows if r["day"] >= start_7d)
    weigh_ins_14d = sum(1 for r in weight_rows if r["day"] >= start_14d)

    days_logged_7d = sum(
        1 for d, row in intake_rows.items() if d >= start_7d and (row.get("meal_count") or 0) > 0
    )
    days_logged_14d = sum(
        1 for d, row in intake_rows.items() if d >= start_14d and (row.get("meal_count") or 0) > 0
    )

    # 3. Baseline physiological estimates (Mifflin-St Jeor)
    latest_weight = float(weight_rows[-1]["weight_kg"]) if weight_rows else float(prof.get("weight_kg") or 75.0)
    height_cm = float(prof.get("height_cm") or 178.0)
    birth_year = int(prof.get("birth_year") or 1995)
    age = max(16, as_of_date.year - birth_year)
    sex = prof.get("sex") or "male"
    act_level = prof.get("activity_level") or "moderate"

    bmr = compute_bmr(sex, latest_weight, height_cm, age)
    baseline_tdee = round(compute_tdee(bmr, act_level))

    # Activity-informed expenditure estimate (BMR + measured burn + TEF)
    measured_burn = avg_active_burn_14d if avg_active_burn_14d > 50 else (avg_steps_14d * 0.04)
    activity_tdee = round(bmr + measured_burn + (avg_intake_14d * 0.10 if 'avg_intake_14d' in locals() else bmr * 0.10))

    # 4. Weight Trend & Rate of Change (Linear Regression on 14-day window)
    weights_14d = [r for r in weight_rows if r["day"] >= start_14d]
    rate_kg_per_week = 0.0
    trend_weight_kg = latest_weight

    if len(weights_14d) >= 3:
        # Convert day strings to integer day offsets relative to start_14d
        base_d = as_of_date - timedelta(days=13)
        pts = [((date.fromisoformat(r["day"]) - base_d).days, float(r["weight_kg"])) for r in weights_14d]
        slope_per_day = _linear_regression_slope(pts)
        rate_kg_per_week = round(slope_per_day * 7.0, 2)
        # Trend weight at as_of_date (day 13)
        avg_x = sum(p[0] for p in pts) / len(pts)
        avg_y = sum(p[1] for p in pts) / len(pts)
        intercept = avg_y - (slope_per_day * avg_x)
        trend_weight_kg = round(intercept + (slope_per_day * 13), 1)
    elif len(weights_14d) == 2:
        d1 = date.fromisoformat(weights_14d[0]["day"])
        d2 = date.fromisoformat(weights_14d[1]["day"])
        days_span = max(1, (d2 - d1).days)
        slope_per_day = (weights_14d[1]["weight_kg"] - weights_14d[0]["weight_kg"]) / days_span
        rate_kg_per_week = round(slope_per_day * 7.0, 2)
        trend_weight_kg = round(weights_14d[-1]["weight_kg"], 1)

    # 5. Adaptive TDEE Calculation
    has_sufficient_data = (days_logged_14d >= 4 and weigh_ins_14d >= 3)
    cals_14d = [
        row["calories"]
        for d, row in intake_rows.items()
        if d >= start_14d and (row.get("meal_count") or 0) > 0 and (row.get("calories") or 0) > 0
    ]
    avg_intake_14d = round(sum(cals_14d) / len(cals_14d)) if cals_14d else baseline_tdee

    cals_7d = [
        row["calories"]
        for d, row in intake_rows.items()
        if d >= start_7d and (row.get("meal_count") or 0) > 0 and (row.get("calories") or 0) > 0
    ]
    avg_intake_7d = round(sum(cals_7d) / len(cals_7d)) if cals_7d else avg_intake_14d

    # Re-evaluate activity TDEE now that avg_intake_14d is resolved
    activity_tdee = round(bmr + measured_burn + (avg_intake_14d * 0.10))

    if has_sufficient_data:
        # Energy deficit or surplus reflected by weight trend:
        # 1 kg fat/tissue loss/gain = ~7,700 kcal
        weekly_tissue_energy = rate_kg_per_week * KCAL_PER_KG_FAT
        daily_stored_energy = weekly_tissue_energy / 7.0
        # True TDEE = Intake - Stored Energy (if losing weight, stored energy is negative, so minus negative = plus)
        raw_adaptive_tdee = avg_intake_14d - daily_stored_energy
        raw_adaptive_tdee = max(1200.0, min(raw_adaptive_tdee, 4500.0))

        # Confidence blending: more logged days = higher trust in empirical data vs formula
        conf_ratio = min(1.0, (days_logged_14d / 10.0) * (weigh_ins_14d / 6.0))
        refined_baseline = round(0.5 * baseline_tdee + 0.5 * activity_tdee) if (avg_steps_14d > 0 or avg_active_burn_14d > 0) else baseline_tdee
        estimated_tdee = round(conf_ratio * raw_adaptive_tdee + (1.0 - conf_ratio) * refined_baseline)
        confidence = "high" if conf_ratio >= 0.75 else "medium"
    else:
        if (avg_steps_14d > 0 or avg_active_burn_14d > 0) and len(act_14d) >= 3:
            estimated_tdee = round(0.4 * baseline_tdee + 0.6 * activity_tdee)
            confidence = "activity_estimated"
        else:
            estimated_tdee = baseline_tdee
            confidence = "insufficient_data"

    # 6. Recommendation based on Goal
    raw_goal = (prof.get("goal") or "cut").strip().lower()
    if "lose" in raw_goal or "cut" in raw_goal:
        goal = "cut"
    elif "gain" in raw_goal or "bulk" in raw_goal:
        goal = "bulk"
    else:
        goal = "maintain"

    goal_rate = float(prof.get("goal_rate_kg_per_week") or (0.5 if goal == "cut" else (0.25 if goal == "bulk" else 0.0)))
    if goal == "maintain":
        goal_rate = 0.0
    elif goal == "cut":
        goal_rate = max(0.1, min(goal_rate, 1.2))
    elif goal == "bulk":
        goal_rate = max(0.05, min(goal_rate, 0.5))

    # Daily caloric target delta for desired pace
    # Rate of 0.5 kg/week = ~550 kcal/day deficit
    target_daily_delta = (goal_rate * KCAL_PER_KG_FAT) / 7.0
    if goal == "cut":
        unclamped_calories = estimated_tdee - target_daily_delta
    elif goal == "bulk":
        unclamped_calories = estimated_tdee + target_daily_delta
    else:
        unclamped_calories = float(estimated_tdee)

    # Physiological safety floor
    safe_floor = 1200.0 if (sex or "").lower() in ("female", "woman", "f") else 1400.0
    current_calories = float(prof.get("calorie_target") or 2200.0)

    # Clamping: smooth weekly step (maximum ±150 kcal shift in a single check-in)
    raw_delta = unclamped_calories - current_calories
    clamped_delta = max(-150.0, min(raw_delta, 150.0))
    new_calories = round(max(safe_floor, current_calories + clamped_delta))

    # Derive macros with ISSN optimal distributions
    # Protein: 2.0 g/kg for cut (muscle preservation), 1.8 g/kg for maintain/bulk
    prot_per_kg = 2.0 if goal == "cut" else 1.8
    new_protein_g = round(latest_weight * prot_per_kg)
    protein_kcal = new_protein_g * 4.0

    # Fat: 25% of energy, floor 20%
    fat_kcal = max(new_calories * 0.20, new_calories * 0.25)
    new_fat_g = round(fat_kcal / 9.0)

    # Carbs: Remainder of energy
    rem_kcal = max(0.0, new_calories - (protein_kcal + (new_fat_g * 9.0)))
    new_carbs_g = round(rem_kcal / 4.0)

    # 7. Check-in Scheduling & Status
    coaching_mode = prof.get("coaching_mode") or "coached"
    coaching_paused = bool(prof.get("coaching_paused"))
    last_checkin_str = prof.get("last_checkin_at")

    days_since_checkin = None
    if last_checkin_str:
        try:
            last_date = date.fromisoformat(last_checkin_str.split("T")[0])
            days_since_checkin = (as_of_date - last_date).days
        except Exception:
            days_since_checkin = None

    if coaching_paused:
        status = "paused"
    elif coaching_mode == "manual":
        status = "manual"
    elif days_since_checkin is not None and days_since_checkin < 7:
        status = "up_to_date"
    elif not has_sufficient_data and (days_logged_7d < 3 or weigh_ins_7d < 2):
        status = "insufficient_data"
    else:
        status = "due"

    next_checkin_date = (
        (date.fromisoformat(last_checkin_str.split("T")[0]) + timedelta(days=7)).isoformat()
        if (days_since_checkin is not None and days_since_checkin < 7)
        else as_of_iso
    )

    # Explanatory summary for the user
    delta_int = round(new_calories - current_calories)
    sign = "+" if delta_int > 0 else ""
    act_desc = f" Active movement: avg {avg_steps_14d:,} steps/day (~{round(avg_active_burn_14d)} kcal/day)." if avg_steps_14d > 0 else ""
    if has_sufficient_data:
        trend_desc = f"{'+' if rate_kg_per_week > 0 else ''}{rate_kg_per_week} kg/wk"
        reasoning = (
            f"Over the last 14 days, you logged {days_logged_14d} days of nutrition (avg {avg_intake_14d} kcal) "
            f"and {weigh_ins_14d} weigh-ins (trend pace: {trend_desc}).{act_desc} "
            f"Your estimated expenditure is ~{estimated_tdee} kcal/day. "
            f"To target your {goal_rate} kg/wk {goal}, we recommend an adjustment of {sign}{delta_int} kcal."
        )
    else:
        reasoning = (
            f"Logged {days_logged_7d}/7 food days and {weigh_ins_7d}/7 weigh-ins this week.{act_desc} "
            f"Using baseline expenditure (~{estimated_tdee} kcal) until more trend data is logged. "
            f"Recommended target is {new_calories} kcal ({sign}{delta_int} kcal)."
        )

    return {
        "status": status,
        "coaching_mode": coaching_mode,
        "coaching_paused": coaching_paused,
        "confidence": confidence,
        "as_of_day": as_of_iso,
        "last_checkin_at": last_checkin_str,
        "next_checkin_date": next_checkin_date,
        "days_since_checkin": days_since_checkin,
        "goal": goal,
        "goal_rate_kg_per_week": goal_rate,
        "estimated_tdee": estimated_tdee,
        "baseline_tdee": baseline_tdee,
        "avg_intake_7d": avg_intake_7d,
        "avg_intake_14d": avg_intake_14d,
        "avg_steps_14d": avg_steps_14d,
        "avg_active_burn_14d": avg_active_burn_14d,
        "trend_weight_kg": trend_weight_kg,
        "rate_kg_per_week": rate_kg_per_week,
        "days_logged_7d": days_logged_7d,
        "days_logged_14d": days_logged_14d,
        "weigh_ins_7d": weigh_ins_7d,
        "weigh_ins_14d": weigh_ins_14d,
        "current_targets": {
            "calories": current_calories,
            "protein_g": float(prof.get("protein_target") or 160.0),
            "carbs_g": float(prof.get("carbs_target") or 220.0),
            "fat_g": float(prof.get("fat_target") or 70.0),
        },
        "recommended_targets": {
            "calories": new_calories,
            "protein_g": new_protein_g,
            "carbs_g": new_carbs_g,
            "fat_g": new_fat_g,
        },
        "delta_calories": delta_int,
        "reasoning": reasoning,
    }


def apply_coaching_checkin(
    conn, profile_id: int, action: str = "apply", custom_calories: float | None = None
) -> dict[str, Any]:
    """Execute a weekly check-in: update profile targets, record in history table, and update timestamp."""
    coaching = calculate_adaptive_coaching(conn, profile_id)
    now_iso = config.now().isoformat()
    today_iso = config.now().date().isoformat()

    old_cals = coaching["current_targets"]["calories"]
    old_pro = coaching["current_targets"]["protein_g"]
    old_car = coaching["current_targets"]["carbs_g"]
    old_fat = coaching["current_targets"]["fat_g"]

    if action == "apply":
        new_cals = coaching["recommended_targets"]["calories"]
        new_pro = coaching["recommended_targets"]["protein_g"]
        new_car = coaching["recommended_targets"]["carbs_g"]
        new_fat = coaching["recommended_targets"]["fat_g"]
        checkin_status = "applied"
    elif action == "customize" and custom_calories is not None:
        new_cals = round(max(1200.0, custom_calories))
        # Re-derive macros
        prof = get_profile(conn, profile_id)
        latest_weight = float(coaching["trend_weight_kg"] or 75.0)
        prot_per_kg = 2.0 if coaching["goal"] == "cut" else 1.8
        new_pro = round(latest_weight * prot_per_kg)
        new_fat = round((new_cals * 0.25) / 9.0)
        new_car = round(max(0.0, new_cals - (new_pro * 4.0 + new_fat * 9.0)) / 4.0)
        checkin_status = "customized"
    elif action == "skip":
        new_cals = old_cals
        new_pro = old_pro
        new_car = old_car
        new_fat = old_fat
        checkin_status = "skipped"
    else:
        raise ValueError(f"Unknown check-in action: {action}")

    # 1. Update Profile targets and check-in timestamp
    conn.execute(
        """UPDATE profiles
           SET calorie_target = ?, protein_target = ?, carbs_target = ?, fat_target = ?,
               last_checkin_at = ?, last_estimated_tdee = ?
           WHERE id = ?""",
        (new_cals, new_pro, new_car, new_fat, today_iso, coaching["estimated_tdee"], profile_id),
    )

    # 2. Record check-in event in history
    conn.execute(
        """INSERT INTO coaching_checkins
               (profile_id, created_at, checkin_day, goal, goal_rate_kg_per_week, estimated_tdee,
                old_calorie_target, new_calorie_target, old_protein_target, new_protein_target,
                old_carbs_target, new_carbs_target, old_fat_target, new_fat_target,
                weight_trend_kg, weight_change_rate, status, reasoning)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            profile_id, now_iso, today_iso, coaching["goal"], coaching["goal_rate_kg_per_week"],
            coaching["estimated_tdee"], old_cals, new_cals, old_pro, new_pro,
            old_car, new_car, old_fat, new_fat, coaching["trend_weight_kg"],
            coaching["rate_kg_per_week"], checkin_status, coaching["reasoning"],
        ),
    )

    return {
        "status": checkin_status,
        "day": today_iso,
        "new_targets": {
            "calories": new_cals,
            "protein_g": new_pro,
            "carbs_g": new_car,
            "fat_g": new_fat,
        },
        "estimated_tdee": coaching["estimated_tdee"],
        "delta_calories": round(new_cals - old_cals),
    }
