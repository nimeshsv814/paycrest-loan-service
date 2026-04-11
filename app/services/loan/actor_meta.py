from __future__ import annotations

from typing import Any

from bson import ObjectId

from ...database.mongo import get_db


def _build_user_queries(actor_id: Any) -> list[dict[str, Any]]:
    queries: list[dict[str, Any]] = []
    if actor_id is None:
        return queries
    queries.append({"_id": actor_id})
    as_str = str(actor_id).strip()
    if as_str:
        queries.append({"_id": as_str})
    if as_str:
        try:
            queries.append({"_id": int(as_str)})
        except Exception:
            pass
        try:
            queries.append({"_id": ObjectId(as_str)})
        except Exception:
            pass
    unique: list[dict[str, Any]] = []
    seen = set()
    for q in queries:
        key = repr(q)
        if key in seen:
            continue
        seen.add(key)
        unique.append(q)
    return unique


async def resolve_actor_meta(actor_id: Any, fallback_role: str | None = None) -> dict[str, Any]:
    db = await get_db()
    user: dict[str, Any] | None = None
    for query in _build_user_queries(actor_id):
        user = await db.staff_users.find_one(query, {"_id": 1, "full_name": 1, "email": 1, "role": 1})
        if user:
            break
    if not user:
        for query in _build_user_queries(actor_id):
            user = await db.users.find_one(query, {"_id": 1, "full_name": 1, "email": 1, "role": 1})
            if user:
                break
    role = str((user or {}).get("role") or fallback_role or "").strip() or None
    full_name = (user or {}).get("full_name")
    email = (user or {}).get("email")
    display = str(full_name or email or actor_id or "").strip() or None
    return {
        "actor_id": actor_id,
        "actor_id_display": str(actor_id) if actor_id is not None else None,
        "actor_name": display,
        "actor_role": role,
    }
