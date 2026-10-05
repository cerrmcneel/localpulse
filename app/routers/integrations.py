"""Mobile health integrations (Google Fit, Android Health Connect, Webhooks)."""
from datetime import date, datetime
import json
import logging
import os
import re
import secrets
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app import auth, config
from app.db import get_conn
from app.deps import get_profile, get_profile_id
from app.models import ActivityManualIn

log = logging.getLogger("tracker")

router = APIRouter(prefix="/api/integrations", tags=["integrations"])


def resolve_integration_profile_id(request: Request, conn) -> int:
    """Validate API key from headers, bearer token, or query parameter."""
    key = (
        request.headers.get("X-API-Key")
        or request.headers.get("X-Integration-Key")
        or request.query_params.get("api_key")
        or request.query_params.get("key")
    )
    if not key:
        auth_hdr = request.headers.get("Authorization", "")
        if auth_hdr.lower().startswith("bearer "):
            key = auth_hdr[7:].strip()

    # 1. Match profile by its personal api_key
    if key:
        row = conn.execute("SELECT id FROM profiles WHERE api_key = ?", (key,)).fetchone()
        if row:
            return row["id"]

    # 2. Match server-wide INTEGRATION_API_KEY or APP_PASSWORD
    configured_key = os.getenv("INTEGRATION_API_KEY", "").strip()
    app_pwd = os.getenv("APP_PASSWORD", "").strip()
    if (configured_key and key == configured_key) or (app_pwd and key == app_pwd):
        return get_profile_id(request, conn, check_pin=False)

    # If an API key was explicitly provided but matched nothing, reject with 401
    if key:
        raise HTTPException(status_code=401, detail="Unauthorized: invalid integration API key.")

    # 3. If the request has an active session cookie (e.g. from the web dashboard)
    cookie = request.cookies.get(auth.COOKIE_NAME)
    if cookie and auth.verify_session(cookie):
        return get_profile_id(request, conn, check_pin=False)

    # 4. If authentication is completely disabled on the instance and no key is set
    if not auth.is_auth_enabled() and not configured_key:
        return get_profile_id(request, conn, check_pin=False)

    raise HTTPException(status_code=401, detail="Unauthorized: missing integration API key.")


def _parse_day_string(val: Any) -> str:
    """Extract YYYY-MM-DD from string or timestamp, or fallback to today."""
    if not val:
        return config.now().date().isoformat()
    if isinstance(val, (int, float)):
        # Could be epoch seconds or millis
        ts = val / 1000.0 if val > 1e11 else val
        try:
            return datetime.fromtimestamp(ts, tz=config.TZ).date().isoformat()
        except Exception:
            return config.now().date().isoformat()
    s = str(val).strip()
    m = re.search(r"(\d{4}-\d{2}-\d{2})", s)
    if m:
        return m.group(1)
    return config.now().date().isoformat()


def _normalize_activity_item(item: dict[str, Any], default_day: str) -> dict[str, Any]:
    """Extract standard activity fields from diverse health sync payloads."""
    day = _parse_day_string(item.get("day") or item.get("date") or item.get("start_time") or default_day)

    # Steps
    raw_steps = item.get("steps") or item.get("step_count") or item.get("steps_count") or 0
    try:
        steps = max(0, int(float(raw_steps)))
    except (ValueError, TypeError):
        steps = 0

    # Active calories burned
    raw_burn = (
        item.get("active_calories")
        or item.get("calories_burned")
        or item.get("active_energy_kcal")
        or item.get("active_kcal")
        or item.get("energy_burned")
        or item.get("active_energy")
        or item.get("calories")
        or 0.0
    )
    try:
        active_calories = max(0.0, float(raw_burn))
    except (ValueError, TypeError):
        active_calories = 0.0

    # Distance in meters
    raw_dist = item.get("distance_m") or item.get("distance_meters") or item.get("distance") or 0.0
    try:
        distance_m = max(0.0, float(raw_dist))
    except (ValueError, TypeError):
        distance_m = 0.0

    # Heart rate average
    raw_hr = item.get("heart_rate_avg") or item.get("heart_rate") or item.get("resting_heart_rate")
    heart_rate_avg = None
    if raw_hr is not None:
        try:
            heart_rate_avg = float(raw_hr)
            if not (30.0 <= heart_rate_avg <= 250.0):
                heart_rate_avg = None
        except (ValueError, TypeError):
            heart_rate_avg = None

    # Sleep minutes
    raw_sleep = item.get("sleep_minutes") or item.get("sleep_duration_min")
    sleep_minutes = None
    if raw_sleep is not None:
        try:
            sleep_minutes = int(float(raw_sleep))
        except (ValueError, TypeError):
            sleep_minutes = None
    elif "sleep_hours" in item:
        try:
            sleep_minutes = int(float(item["sleep_hours"]) * 60)
        except (ValueError, TypeError):
            sleep_minutes = None

    # Weight in kg (if smart scale synced through Google Fit)
    raw_weight = item.get("weight_kg") or item.get("weight")
    weight_kg = None
    if raw_weight is not None:
        try:
            weight_kg = float(raw_weight)
            if not (20.0 <= weight_kg <= 500.0):
                weight_kg = None
        except (ValueError, TypeError):
            weight_kg = None

    source = str(item.get("source") or "health_connect")[:60]

    return {
        "day": day,
        "steps": steps,
        "active_calories": round(active_calories, 1),
        "distance_m": round(distance_m, 1),
        "heart_rate_avg": round(heart_rate_avg, 1) if heart_rate_avg is not None else None,
        "sleep_minutes": sleep_minutes,
        "weight_kg": round(weight_kg, 2) if weight_kg is not None else None,
        "source": source,
        "raw_json": json.dumps(item)[:2000],
    }


def _upsert_activity(conn, profile_id: int, record: dict[str, Any], now_iso: str) -> None:
    """Store or update a daily activity row and optional weight."""
    conn.execute(
        """INSERT INTO daily_activities
               (profile_id, day, steps, active_calories, distance_m, heart_rate_avg,
                sleep_minutes, source, updated_at, raw_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(profile_id, day) DO UPDATE SET
               steps = excluded.steps,
               active_calories = excluded.active_calories,
               distance_m = excluded.distance_m,
               heart_rate_avg = COALESCE(excluded.heart_rate_avg, daily_activities.heart_rate_avg),
               sleep_minutes = COALESCE(excluded.sleep_minutes, daily_activities.sleep_minutes),
               source = excluded.source,
               updated_at = excluded.updated_at,
               raw_json = excluded.raw_json""",
        (
            profile_id,
            record["day"],
            record["steps"],
            record["active_calories"],
            record["distance_m"],
            record["heart_rate_avg"],
            record["sleep_minutes"],
            record["source"],
            now_iso,
            record["raw_json"],
        ),
    )

    if record.get("weight_kg"):
        conn.execute(
            """INSERT INTO weights (profile_id, day, logged_at, weight_kg, notes)
               VALUES (?, ?, ?, ?, 'Synced from mobile')
               ON CONFLICT(profile_id, day) DO UPDATE SET
                   weight_kg = excluded.weight_kg,
                   logged_at = excluded.logged_at""",
            (profile_id, record["day"], now_iso, record["weight_kg"]),
        )


@router.post("/health")
@router.post("/google-fit")
@router.post("/health-connect")
async def ingest_health_data(request: Request) -> dict[str, Any]:
    """Ingest daily steps, active calories, sleep, and weight from mobile health bridges."""
    with get_conn() as conn:
        profile_id = resolve_integration_profile_id(request, conn)

        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON body.")

        default_day = config.now().date().isoformat()
        now_iso = config.now().isoformat()

        # Handle various list and object shapes
        records_to_process: list[dict[str, Any]] = []
        if isinstance(body, list):
            for entry in body:
                if isinstance(entry, dict):
                    records_to_process.append(_normalize_activity_item(entry, default_day))
        elif isinstance(body, dict):
            if "records" in body and isinstance(body["records"], list):
                for entry in body["records"]:
                    if isinstance(entry, dict):
                        records_to_process.append(_normalize_activity_item(entry, default_day))
            elif "data" in body and isinstance(body["data"], list):
                for entry in body["data"]:
                    if isinstance(entry, dict):
                        records_to_process.append(_normalize_activity_item(entry, default_day))
            else:
                records_to_process.append(_normalize_activity_item(body, default_day))

        if not records_to_process:
            raise HTTPException(status_code=400, detail="No valid health records provided in request.")

        for r in records_to_process:
            _upsert_activity(conn, profile_id, r, now_iso)

        latest = records_to_process[-1]
        log.info(
            "Ingested %d activity record(s) for profile=%d (day=%s steps=%d burn=%s)",
            len(records_to_process),
            profile_id,
            latest["day"],
            latest["steps"],
            latest["active_calories"],
        )

        return {
            "status": "ok",
            "profile_id": profile_id,
            "processed_count": len(records_to_process),
            "latest": {
                "day": latest["day"],
                "steps": latest["steps"],
                "active_calories": latest["active_calories"],
                "distance_m": latest["distance_m"],
                "source": latest["source"],
            },
        }


@router.get("/config")
def get_integration_config(request: Request) -> dict[str, Any]:
    """Retrieve integration webhook credentials and instructions for the active profile."""
    with get_conn() as conn:
        profile_id = get_profile_id(request, conn)
        prof = get_profile(conn, profile_id)
        api_key = prof.get("api_key")
        if not api_key:
            api_key = f"lp_{secrets.token_hex(16)}"
            conn.execute("UPDATE profiles SET api_key = ? WHERE id = ?", (api_key, profile_id))

        last_activity = conn.execute(
            """SELECT * FROM daily_activities
               WHERE profile_id = ?
               ORDER BY day DESC, updated_at DESC LIMIT 1""",
            (profile_id,),
        ).fetchone()

        base_url = str(request.base_url).rstrip("/")
        webhook_url = f"{base_url}/api/integrations/health"

        return {
            "profile_id": profile_id,
            "profile_name": prof.get("name"),
            "api_key": api_key,
            "webhook_url": webhook_url,
            "last_sync": {
                "day": last_activity["day"],
                "steps": last_activity["steps"],
                "active_calories": last_activity["active_calories"],
                "source": last_activity["source"],
                "updated_at": last_activity["updated_at"],
            } if last_activity else None,
        }


@router.post("/regenerate-key")
def regenerate_integration_key(request: Request) -> dict[str, Any]:
    """Generate a new API key for the active profile."""
    with get_conn() as conn:
        profile_id = get_profile_id(request, conn)
        new_key = f"lp_{secrets.token_hex(16)}"
        conn.execute("UPDATE profiles SET api_key = ? WHERE id = ?", (new_key, profile_id))
        return {"status": "ok", "api_key": new_key}


@router.post("/manual")
def log_manual_activity(request: Request, payload: ActivityManualIn) -> dict[str, Any]:
    """Manually enter or adjust daily steps and active calories from the web UI."""
    with get_conn() as conn:
        profile_id = get_profile_id(request, conn)
        day_str = (payload.day or config.now().date()).isoformat()
        now_iso = config.now().isoformat()

        record = {
            "day": day_str,
            "steps": payload.steps,
            "active_calories": payload.active_calories,
            "distance_m": payload.distance_m,
            "heart_rate_avg": payload.heart_rate_avg,
            "sleep_minutes": payload.sleep_minutes,
            "weight_kg": None,
            "source": "manual",
            "raw_json": json.dumps(payload.model_dump(mode="json")),
        }
        _upsert_activity(conn, profile_id, record, now_iso)

        return {
            "status": "ok",
            "day": day_str,
            "steps": payload.steps,
            "active_calories": payload.active_calories,
        }
