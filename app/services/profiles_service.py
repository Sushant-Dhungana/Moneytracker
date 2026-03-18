from datetime import date

from psycopg import Connection
from psycopg import Error as PsycopgError

from app.core.errors import ApiError
from app.repositories.profiles_repository import (
    get_business_setup_state,
    get_user_active_profile_id,
    list_profiles,
    set_user_active_profile_id,
    switch_active_profile,
    upsert_business_profile_with_opening,
    upsert_personal_profile,
)


def _fallback_personal_name(email: str | None) -> str:
    prefix = (email or "").split("@")[0].strip()
    return prefix or "Personal"


def ensure_personal_profile(conn: Connection, *, user_id: str, email: str | None) -> dict:
    return upsert_personal_profile(conn, user_id=user_id, name=_fallback_personal_name(email))


def fetch_active_profile_state(conn: Connection, *, user_id: str, email: str | None) -> dict:
    profiles = list_profiles(conn, user_id=user_id)
    if not profiles:
        ensure_personal_profile(conn, user_id=user_id, email=email)
        profiles = list_profiles(conn, user_id=user_id)

    active_profile_id = get_user_active_profile_id(conn, user_id=user_id)
    active_profile = None
    if active_profile_id:
        active_profile = next(
            (
                item
                for item in profiles
                if str(item.get("id")) == str(active_profile_id)
            ),
            None,
        )

    if not active_profile:
        active_profile = next((item for item in profiles if item["profile_type"] == "personal"), None)
        if not active_profile and profiles:
            active_profile = profiles[0]
        if active_profile:
            set_user_active_profile_id(
                conn,
                user_id=user_id,
                profile_id=str(active_profile["id"]),
            )
            active_profile_id = str(active_profile["id"])

    setup_state = get_business_setup_state(conn, user_id=user_id)
    has_business_profile = bool(
        setup_state.get("has_business_profile")
        if setup_state
        else any(item["profile_type"] == "business" for item in profiles)
    )

    return {
        "profiles": profiles,
        "activeProfileId": str(active_profile_id) if active_profile_id else None,
        "activeProfileType": (active_profile or {}).get("profile_type", "personal"),
        "hasBusinessProfile": has_business_profile,
        "hasBusinessSetupComplete": bool(setup_state.get("has_business_setup_complete")),
    }


def switch_profile(conn: Connection, *, user_id: str, target_type: str) -> dict:
    profiles = list_profiles(conn, user_id=user_id)
    target = next((item for item in profiles if item["profile_type"] == target_type), None)
    if not target:
        return {"status": "missing_profile", "profile": None}

    try:
        switch_active_profile(conn, user_id=user_id, target_type=target_type)
    except PsycopgError as exc:
        message = str(exc)
        if "business_setup_incomplete" in message.lower():
            return {"status": "missing_setup", "profile": None}
        raise ApiError(status_code=400, code="profile_switch_failed", message=message) from exc

    return {"status": "switched", "profile": target}


def upsert_business_profile(
    conn: Connection,
    *,
    user_id: str,
    name: str,
    phone_number: str | None,
    address: str | None,
    pan_number: str | None,
    opening_balance: float | None,
) -> dict:
    try:
        return upsert_business_profile_with_opening(
            conn,
            user_id=user_id,
            name=name.strip(),
            phone_number=(phone_number or "").strip() or None,
            address=(address or "").strip() or None,
            pan_number=(pan_number or "").strip() or None,
            opening_balance=opening_balance,
            opening_date=date.today().isoformat(),
        )
    except PsycopgError as exc:
        raise ApiError(
            status_code=400,
            code="business_profile_upsert_failed",
            message=str(exc),
        ) from exc
