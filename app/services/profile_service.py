from psycopg import Connection

from app.core.errors import ApiError
from app.repositories.profile_repository import (
    complete_profile,
    get_profile_summary,
    update_profile_name,
)


def fetch_profile_summary(conn: Connection, user_id: str) -> dict:
    profile = get_profile_summary(conn, user_id)
    if not profile:
        raise ApiError(status_code=404, code="profile_not_found", message="Profile not found.")
    return profile


def update_user_profile_name(
    conn: Connection, *, user_id: str, first_name: str, last_name: str
) -> dict:
    updated = update_profile_name(
        conn, user_id=user_id, first_name=first_name.strip(), last_name=last_name.strip()
    )
    if not updated:
        raise ApiError(status_code=404, code="profile_not_found", message="Profile not found.")
    return updated


def complete_user_profile(
    conn: Connection,
    *,
    user_id: str,
    first_name: str,
    last_name: str,
    country_code: str,
    currency_code: str,
) -> dict:
    updated = complete_profile(
        conn,
        user_id=user_id,
        first_name=first_name.strip(),
        last_name=last_name.strip(),
        country_code=country_code.strip().upper(),
        currency_code=currency_code.strip().upper(),
    )
    if not updated:
        raise ApiError(status_code=404, code="profile_not_found", message="Profile not found.")
    return updated
