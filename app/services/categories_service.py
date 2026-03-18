from psycopg import Connection
from psycopg import Error as PsycopgError

from app.core.errors import ApiError
from app.repositories.categories_repository import (
    get_active_profile_id,
    insert_category,
    list_categories,
    remove_category,
    upsert_default_categories,
)


def _resolve_profile_id(conn: Connection, user_id: str) -> str:
    profile_id = get_active_profile_id(conn, user_id)
    if not profile_id:
        raise ApiError(
            status_code=400,
            code="missing_active_profile",
            message="No active profile found for categories.",
        )
    return profile_id


def fetch_categories(conn: Connection, user_id: str) -> list[dict]:
    profile_id = _resolve_profile_id(conn, user_id)
    return list_categories(conn, user_id, profile_id)


def create_category(
    conn: Connection,
    *,
    user_id: str,
    name: str,
    cat_type: str,
    parent_id: str | None,
    icon: str | None,
    color: str | None,
) -> dict:
    profile_id = _resolve_profile_id(conn, user_id)
    try:
        return insert_category(
            conn,
            user_id=user_id,
            profile_id=profile_id,
            name=name.strip(),
            cat_type=cat_type,
            parent_id=parent_id,
            icon=icon,
            color=color,
        )
    except PsycopgError as exc:
        raise ApiError(status_code=400, code="category_create_failed", message=str(exc).strip()) from exc


def delete_category(conn: Connection, *, user_id: str, category_id: str) -> None:
    profile_id = _resolve_profile_id(conn, user_id)
    try:
        remove_category(conn, user_id=user_id, profile_id=profile_id, category_id=category_id)
    except PsycopgError as exc:
        raise ApiError(status_code=400, code="category_delete_failed", message=str(exc).strip()) from exc


def ensure_default_categories(conn: Connection, *, user_id: str) -> None:
    profile_id = _resolve_profile_id(conn, user_id)
    try:
        upsert_default_categories(conn, user_id=user_id, profile_id=profile_id)
    except PsycopgError as exc:
        raise ApiError(status_code=400, code="category_seed_failed", message=str(exc).strip()) from exc

