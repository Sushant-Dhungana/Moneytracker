from fastapi import APIRouter, Depends
from psycopg import Connection

from app.core.auth import AuthContext, get_auth_context
from app.core.db import apply_db_auth_context, get_db_conn
from app.schemas.profiles import (
    ActiveProfileStateResponse,
    ProfileRow,
    SwitchProfileRequest,
    SwitchProfileResponse,
    UpsertBusinessProfileRequest,
)
from app.services.profiles_service import (
    ensure_personal_profile,
    fetch_active_profile_state,
    switch_profile,
    upsert_business_profile,
)

router = APIRouter(prefix="/profiles", tags=["profiles"])


@router.get("", response_model=list[ProfileRow])
def get_profiles(
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> list[ProfileRow]:
    apply_db_auth_context(conn, auth.user_id)
    state = fetch_active_profile_state(conn, user_id=auth.user_id, email=auth.email)
    return [ProfileRow(**item) for item in state["profiles"]]


@router.get("/state", response_model=ActiveProfileStateResponse)
def get_profiles_state(
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> ActiveProfileStateResponse:
    apply_db_auth_context(conn, auth.user_id)
    state = fetch_active_profile_state(conn, user_id=auth.user_id, email=auth.email)
    return ActiveProfileStateResponse(**state)


@router.post("/personal/ensure", response_model=ProfileRow)
def post_ensure_personal_profile(
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> ProfileRow:
    apply_db_auth_context(conn, auth.user_id)
    row = ensure_personal_profile(conn, user_id=auth.user_id, email=auth.email)
    return ProfileRow(**row)


@router.post("/switch", response_model=SwitchProfileResponse)
def post_switch_profile(
    payload: SwitchProfileRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> SwitchProfileResponse:
    apply_db_auth_context(conn, auth.user_id)
    result = switch_profile(conn, user_id=auth.user_id, target_type=payload.target_type)
    profile = ProfileRow(**result["profile"]) if result.get("profile") else None
    return SwitchProfileResponse(status=result["status"], profile=profile)


@router.post("/business/upsert", response_model=ProfileRow)
def post_upsert_business_profile(
    payload: UpsertBusinessProfileRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> ProfileRow:
    apply_db_auth_context(conn, auth.user_id)
    row = upsert_business_profile(
        conn,
        user_id=auth.user_id,
        name=payload.name,
        phone_number=payload.phone_number,
        address=payload.address,
        pan_number=payload.pan_number,
        opening_balance=payload.opening_balance,
    )
    return ProfileRow(**row)
