from fastapi import APIRouter, Depends
from psycopg import Connection

from app.core.auth import AuthContext, get_auth_context
from app.core.db import apply_db_auth_context, get_db_conn
from app.schemas.common import (
    ApiResponse,
    ProfileCompleteRequest,
    ProfileCompleteResponse,
    ProfileNameUpdateRequest,
    ProfileNameUpdateResponse,
    ProfileSummary,
)
from app.services.profile_service import (
    complete_user_profile,
    fetch_profile_summary,
    update_user_profile_name,
)

router = APIRouter(prefix="/profile", tags=["profile"])


@router.get("", response_model=ProfileSummary)
def get_profile(
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> ProfileSummary:
    apply_db_auth_context(conn, auth.user_id)
    row = fetch_profile_summary(conn, auth.user_id)
    return ProfileSummary(**row)


@router.patch("/name", response_model=ProfileNameUpdateResponse)
def patch_profile_name(
    payload: ProfileNameUpdateRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> ProfileNameUpdateResponse:
    apply_db_auth_context(conn, auth.user_id)
    updated = update_user_profile_name(
        conn,
        user_id=auth.user_id,
        first_name=payload.first_name,
        last_name=payload.last_name,
    )
    return ProfileNameUpdateResponse(**updated)


@router.patch("/complete", response_model=ProfileCompleteResponse)
def patch_profile_complete(
    payload: ProfileCompleteRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> ProfileCompleteResponse:
    apply_db_auth_context(conn, auth.user_id)
    updated = complete_user_profile(
        conn,
        user_id=auth.user_id,
        first_name=payload.first_name,
        last_name=payload.last_name,
        country_code=payload.country_code,
        currency_code=payload.currency_code,
    )
    return ProfileCompleteResponse(**updated)


@router.post("/ensure", response_model=ApiResponse)
def post_profile_ensure(
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> ApiResponse:
    apply_db_auth_context(conn, auth.user_id)
    email = (
        str(auth.claims.get("email", "")).strip()
        if isinstance(auth.claims, dict)
        else ""
    )
    username = email.split("@")[0].strip() if email else None
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into public.user_profiles (id, email, username)
            values (%s::uuid, %s::text, %s::text)
            on conflict (id)
            do update set
              email = excluded.email,
              username = coalesce(public.user_profiles.username, excluded.username)
            """,
            (auth.user_id, email or None, username or None),
        )
        cur.execute(
            """
            insert into public.profiles (user_id, profile_type, name)
            values (
              %s::uuid,
              'personal',
              coalesce(nullif(%s::text, ''), 'Personal')
            )
            on conflict (user_id, profile_type)
            do update set name = excluded.name
            returning id::text as id
            """,
            (auth.user_id, username or "Personal"),
        )
        personal = cur.fetchone()
        if personal and personal.get("id"):
            cur.execute(
                """
                update public.user_profiles
                   set active_profile_id = coalesce(active_profile_id, %s::uuid)
                 where id = %s::uuid
                """,
                (personal["id"], auth.user_id),
            )
    return ApiResponse(ok=True)


@router.patch("/avatar", response_model=ApiResponse)
def patch_profile_avatar(
    payload: dict,
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> ApiResponse:
    apply_db_auth_context(conn, auth.user_id)
    avatar_url = payload.get("avatar_url")
    email = payload.get("email")
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into public.user_profiles (id, email, avatar_url)
            values (%s::uuid, %s::text, %s::text)
            on conflict (id)
            do update set
              email = coalesce(excluded.email, public.user_profiles.email),
              avatar_url = excluded.avatar_url
            """,
            (
                auth.user_id,
                str(email).strip() if email is not None else None,
                str(avatar_url).strip() if avatar_url is not None else None,
            ),
        )
    return ApiResponse(ok=True)
