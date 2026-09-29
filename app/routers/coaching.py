"""Adaptive macro coaching endpoints."""
from typing import Any
from fastapi import APIRouter, HTTPException, Request

from app.db import get_conn
from app.deps import get_profile_id
from app.models import CoachingCheckinIn, CoachingSettingsUpdate
from app.services.coaching import calculate_adaptive_coaching, apply_coaching_checkin

router = APIRouter(prefix="/api/coaching", tags=["coaching"])


@router.get("/status")
def coaching_status(request: Request) -> dict[str, Any]:
    """Retrieve current adaptive metabolic expenditure, targets, and weekly check-in readiness."""
    with get_conn() as conn:
        profile_id = get_profile_id(request, conn)
        return calculate_adaptive_coaching(conn, profile_id)


@router.post("/checkin")
def submit_checkin(request: Request, payload: CoachingCheckinIn) -> dict[str, Any]:
    """Apply, customize, or skip the weekly adaptive check-in."""
    with get_conn() as conn:
        profile_id = get_profile_id(request, conn)

        # If goal/rate overrides are provided with customize, update profile first
        updates = {}
        if payload.goal:
            updates["goal"] = payload.goal
        if payload.goal_rate_kg_per_week is not None:
            updates["goal_rate_kg_per_week"] = payload.goal_rate_kg_per_week
        if updates:
            cols = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(f"UPDATE profiles SET {cols} WHERE id = ?", (*updates.values(), profile_id))

        res = apply_coaching_checkin(
            conn, profile_id, action=payload.action, custom_calories=payload.custom_calories
        )
        return res


@router.patch("/settings")
def update_coaching_settings(request: Request, payload: CoachingSettingsUpdate) -> dict[str, Any]:
    """Update profile goal (cut/maintain/bulk), target rate, and coaching mode."""
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields provided to update.")

    with get_conn() as conn:
        profile_id = get_profile_id(request, conn)
        if "coaching_paused" in fields:
            fields["coaching_paused"] = 1 if fields["coaching_paused"] else 0

        cols = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE profiles SET {cols} WHERE id = ?", (*fields.values(), profile_id))
        return calculate_adaptive_coaching(conn, profile_id)


@router.get("/history")
def coaching_history(request: Request, limit: int = 12) -> dict[str, Any]:
    """Retrieve past check-in decisions and TDEE progression."""
    with get_conn() as conn:
        profile_id = get_profile_id(request, conn)
        rows = conn.execute(
            """SELECT * FROM coaching_checkins
               WHERE profile_id = ?
               ORDER BY created_at DESC LIMIT ?""",
            (profile_id, max(1, min(limit, 50))),
        ).fetchall()
        return {"checkins": [dict(r) for r in rows]}
