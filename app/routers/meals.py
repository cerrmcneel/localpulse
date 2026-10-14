"""Meal analysis and logging.

Analysis and logging are deliberately separate calls: /analyze never writes to the
database, so the user always gets an editable card to correct before anything is
committed. The photo is parked in _pending/ and claimed by token on confirm.
"""
from datetime import date, datetime

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from app import config
from app.db import get_conn
from app.deps import get_profile_id
from app.models import MealIn, MealItemIn, MealUpdate, TextAnalysisIn
from app.services import images, vision

router = APIRouter(prefix="/api", tags=["meals"])


@router.post("/analyze")
async def analyze(
    image: UploadFile | None = File(default=None),
    text: str | None = Form(default=None),
    model: str | None = Form(default=None),
):
    """Estimate macros from a photo, natural language description, or BOTH."""
    clean_text = (text or "").strip()

    img = None
    b64 = None
    if image and image.filename:
        try:
            content = await image.read()
            if content:
                img = images.open_image(content)
                b64 = images.prepare_for_vision(img)
        except images.ImageError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not b64 and not clean_text:
        raise HTTPException(
            status_code=400,
            detail="Please provide a meal photo, a text description, or both.",
        )

    try:
        if b64 and clean_text:
            result = await vision.analyze_meal_multimodal(image_b64=b64, text=clean_text, model=model)
            source = "photo"
        elif b64:
            result = await vision.analyze_meal(b64, model=model)
            source = "photo"
        else:
            result = await vision.analyze_meal_text(clean_text, model=model)
            source = "text"
    except vision.VisionError as exc:
        # 502: the failure is in the upstream model, not in the client's request.
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if not result["items"]:
        raise HTTPException(
            status_code=422,
            detail="The model could not identify any food items. Try a clearer shot or more details.",
        )

    result["source"] = source
    if img:
        result["pending_image"] = images.save_pending(img)
    else:
        result["pending_image"] = None

    return result


@router.post("/analyze-text")
async def analyze_text(payload: TextAnalysisIn):
    """Estimate macros from a natural language meal description."""
    try:
        result = await vision.analyze_meal_text(payload.text, model=payload.model)
    except vision.VisionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if not result["items"]:
        raise HTTPException(
            status_code=422,
            detail="The model could not identify any food items in your description. Please try describing your meal in more detail.",
        )

    result["pending_image"] = None
    return result


def infer_meal_type(dt: datetime | None = None) -> str:
    """Infer meal slot from time of day tailored to Spanish schedules:
    - Breakfast: 06:00 - 12:30
    - Lunch (comida): 12:30 - 16:00 (covers Spain's 13:00 - 15:30)
    - Merienda / Snack: 16:00 - 20:00
    - Dinner (cena): 20:00 - 24:00 (covers Spain's 20:00 - 23:00+)
    - Late-night: 00:00 - 06:00 -> snack
    """
    now = dt or config.now()
    h = now.hour + now.minute / 60.0
    if 6.0 <= h < 12.5:
        return "breakfast"
    elif 12.5 <= h < 16.0:
        return "lunch"
    elif 16.0 <= h < 20.0:
        return "snack"
    elif 20.0 <= h <= 24.0:
        return "dinner"
    return "snack"


@router.post("/meals", status_code=201)
def create_meal(request: Request, meal: MealIn):
    """Commit a reviewed meal, claiming its pending photo if one was supplied."""
    when = config.now()
    target_date = meal.day or when.date()
    day = target_date.isoformat()

    # Retain the time-of-day on the target date so the meal renders at the right time
    logged_at_dt = datetime.combine(target_date, when.time()).replace(tzinfo=when.tzinfo) if meal.day else when
    logged_at = logged_at_dt.isoformat()

    image_path = None
    if meal.pending_image:
        image_path = images.commit_pending(meal.pending_image, logged_at_dt)

    meal_type = meal.meal_type
    if not meal_type or meal_type == "other":
        meal_type = infer_meal_type(logged_at_dt)

    with get_conn() as conn:
        profile_id = get_profile_id(request, conn)
        cur = conn.execute(
            """INSERT INTO meals (profile_id, day, logged_at, name, meal_type, source,
                                  image_path, model, notes, raw_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (profile_id, day, logged_at, meal.name, meal_type, meal.source,
             image_path, meal.model, meal.notes, meal.raw_json),
        )
        meal_id = cur.lastrowid
        _insert_items(conn, meal_id, meal.items)
        return _fetch_meal(conn, meal_id)


@router.get("/meals")
def list_meals(request: Request, day: date | None = None, limit: int = 100):
    """Meals for a given day (defaults to today) for the active profile, newest first."""
    target = (day or config.now().date()).isoformat()
    with get_conn() as conn:
        profile_id = get_profile_id(request, conn)
        rows = conn.execute(
            "SELECT id FROM meals WHERE profile_id = ? AND day = ? ORDER BY logged_at DESC LIMIT ?",
            (profile_id, target, max(1, min(limit, 500))),
        ).fetchall()
        return {"day": target, "meals": [_fetch_meal(conn, r["id"]) for r in rows]}


@router.get("/meals/search")
def search_meals(
    request: Request,
    q: str = "",
    limit: int = 40,
    meal_type: str | None = None,
    sort: str = "recent",
):
    """Search historical meals by name, notes, or ingredients for fast repeat."""
    clean_q = q.strip()
    limit = max(1, min(limit, 100))
    with get_conn() as conn:
        profile_id = get_profile_id(request, conn)

        params: list = [profile_id]
        where_clauses = ["m.profile_id = ?"]

        if clean_q:
            where_clauses.append(
                """(
                    m.name LIKE ?
                    OR m.notes LIKE ?
                    OR EXISTS (
                        SELECT 1 FROM meal_items mi
                        WHERE mi.meal_id = m.id AND mi.name LIKE ?
                    )
                )"""
            )
            pattern = f"%{clean_q}%"
            params.extend([pattern, pattern, pattern])

        if meal_type and meal_type in {"breakfast", "lunch", "dinner", "snack", "other"}:
            where_clauses.append("m.meal_type = ?")
            params.append(meal_type)

        where_sql = " AND ".join(where_clauses)
        candidate_limit = 300 if not clean_q else 150
        sql = f"""
            SELECT m.id
            FROM meals m
            WHERE {where_sql}
            ORDER BY m.logged_at DESC
            LIMIT ?
        """
        params.append(candidate_limit)
        rows = conn.execute(sql, params).fetchall()

        candidates = [_fetch_meal(conn, r["id"]) for r in rows if r]

        # Deduplicate / group recurring meals by normalized signature:
        # signature: (normalized meal name, sorted tuple of normalized item names)
        groups: dict[tuple, dict] = {}
        for meal in candidates:
            if not meal:
                continue
            norm_name = meal["name"].strip().lower()
            sorted_items = tuple(sorted(it["name"].strip().lower() for it in meal.get("items", [])))
            sig = (norm_name, sorted_items)

            if sig not in groups:
                m_entry = dict(meal)
                m_entry["times_logged"] = 1
                m_entry["last_logged_at"] = meal["logged_at"]
                m_entry["last_day"] = meal["day"]
                groups[sig] = m_entry
            else:
                groups[sig]["times_logged"] += 1
                if not groups[sig].get("image_url") and meal.get("image_url"):
                    groups[sig]["image_url"] = meal["image_url"]
                    groups[sig]["image_path"] = meal["image_path"]

        results = list(groups.values())
        if sort == "frequent":
            results.sort(key=lambda m: (m["times_logged"], m["last_logged_at"]), reverse=True)
        else:
            results.sort(key=lambda m: m["last_logged_at"], reverse=True)

        results = results[:limit]
        return {"query": clean_q, "meals": results, "total": len(results)}


@router.get("/meals/{meal_id}")
def get_meal(request: Request, meal_id: int):
    with get_conn() as conn:
        profile_id = get_profile_id(request, conn)
        meal = _fetch_meal(conn, meal_id)
        if meal is None or meal.get("profile_id") != profile_id:
            raise HTTPException(status_code=404, detail="Meal not found.")
        return meal


@router.post("/meals/{meal_id}/duplicate", status_code=201)
def duplicate_meal(request: Request, meal_id: int, day: date | None = None):
    """Log this meal again today (or specified day) -- duplicates items and macros for fast meal reuse."""
    when = config.now()
    target_date = day or when.date()
    target_day = target_date.isoformat()
    logged_dt = datetime.combine(target_date, when.time()).replace(tzinfo=when.tzinfo) if day else when
    with get_conn() as conn:
        profile_id = get_profile_id(request, conn)
        src = _fetch_meal(conn, meal_id)
        if src is None or src.get("profile_id") != profile_id:
            raise HTTPException(status_code=404, detail="Source meal not found.")

        cur = conn.execute(
            """INSERT INTO meals (profile_id, day, logged_at, name, meal_type, source,
                                  image_path, model, notes, raw_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (profile_id, target_day, logged_dt.isoformat(), src["name"], infer_meal_type(logged_dt), "manual",
             src["image_path"], src["model"], src["notes"], None),
        )
        new_id = cur.lastrowid
        item_objs = [MealItemIn(**it) for it in src["items"]]
        _insert_items(conn, new_id, item_objs)
        return _fetch_meal(conn, new_id)


@router.patch("/meals/{meal_id}")
def update_meal(request: Request, meal_id: int, patch: MealUpdate):
    with get_conn() as conn:
        profile_id = get_profile_id(request, conn)
        if conn.execute("SELECT 1 FROM meals WHERE id = ? AND profile_id = ?", (meal_id, profile_id)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Meal not found.")

        fields = patch.model_dump(exclude_unset=True, exclude={"items"})
        if "day" in fields and fields["day"] is not None:
            fields["day"] = fields["day"].isoformat()
        if fields:
            assignments = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(
                f"UPDATE meals SET {assignments} WHERE id = ? AND profile_id = ?",
                (*fields.values(), meal_id, profile_id),
            )

        if patch.items is not None:
            if not patch.items:
                raise HTTPException(status_code=400, detail="A meal needs at least one item.")
            conn.execute("DELETE FROM meal_items WHERE meal_id = ?", (meal_id,))
            _insert_items(conn, meal_id, patch.items)

        return _fetch_meal(conn, meal_id)


@router.delete("/meals/{meal_id}", status_code=204)
def delete_meal(request: Request, meal_id: int):
    """Remove a meal. The photo on disk is kept -- deleting a mislogged entry
    should not silently destroy the only copy of the picture."""
    with get_conn() as conn:
        profile_id = get_profile_id(request, conn)
        cur = conn.execute("DELETE FROM meals WHERE id = ? AND profile_id = ?", (meal_id, profile_id))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Meal not found.")


def _insert_items(conn, meal_id: int, items) -> None:
    conn.executemany(
        """INSERT INTO meal_items
               (meal_id, name, grams, calories, protein_g, carbs_g, fat_g, confidence, position)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (meal_id, it.name, round(float(it.grams), 1), round(float(it.calories), 1),
             round(float(it.protein_g), 1), round(float(it.carbs_g), 1), round(float(it.fat_g), 1),
             it.confidence, pos)
            for pos, it in enumerate(items)
        ],
    )


def _fetch_meal(conn, meal_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM meals WHERE id = ?", (meal_id,)).fetchone()
    if row is None:
        return None
    items = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM meal_items WHERE meal_id = ? ORDER BY position", (meal_id,)
        )
    ]
    meal = dict(row)
    meal["items"] = items
    meal["totals"] = vision.totals_for(items)
    meal["image_url"] = f"/media/{row['image_path']}" if row["image_path"] else None
    return meal
