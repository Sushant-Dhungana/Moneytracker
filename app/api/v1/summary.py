from datetime import date
from time import perf_counter

from fastapi import APIRouter, Depends, Query
from psycopg import Connection

from app.core.auth import AuthContext, get_auth_context
from app.core.db import apply_db_auth_context, get_db_conn
from app.schemas.summary import (
    BusinessDueSummaryResponse,
    BusinessSummaryResponse,
    PersonalSummaryResponse,
)
from app.services.summary_service import (
    fetch_business_due_summary,
    fetch_business_summary,
    fetch_personal_summary,
)

router = APIRouter(tags=["summary"])


@router.get("/personal/summary", response_model=PersonalSummaryResponse)
def get_personal_summary(
    profile_id: str = Query(...),
    period: str = Query(default="all"),
    from_date: date | None = Query(default=None),
    to_date: date | None = Query(default=None),
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> PersonalSummaryResponse:
    apply_db_auth_context(conn, auth.user_id)
    payload = fetch_personal_summary(
        conn,
        user_id=auth.user_id,
        profile_id=profile_id,
        period=period,
        from_date=from_date,
        to_date=to_date,
    )
    return PersonalSummaryResponse(**payload)


@router.get("/business/summary", response_model=BusinessSummaryResponse)
def get_business_summary(
    profile_id: str = Query(...),
    period: str = Query(default="all"),
    from_date: date | None = Query(default=None),
    to_date: date | None = Query(default=None),
    include_due_snapshot: bool = Query(default=False),
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> BusinessSummaryResponse:
    apply_db_auth_context(conn, auth.user_id)
    payload = fetch_business_summary(
        conn,
        user_id=auth.user_id,
        profile_id=profile_id,
        period=period,
        from_date=from_date,
        to_date=to_date,
        include_due_snapshot=include_due_snapshot,
    )
    return BusinessSummaryResponse(**payload)


@router.get("/business/due-summary", response_model=BusinessDueSummaryResponse)
def get_business_due_summary(
    profile_id: str = Query(...),
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> BusinessDueSummaryResponse:
    endpoint_started_at = perf_counter()
    apply_db_auth_context(conn, auth.user_id)
    fetch_started_at = perf_counter()
    payload = fetch_business_due_summary(
        conn,
        user_id=auth.user_id,
        profile_id=profile_id,
    )
    fetch_ms = (perf_counter() - fetch_started_at) * 1000
    total_ms = (perf_counter() - endpoint_started_at) * 1000
    print(
        "[Perf] api GET /business/due-summary stages:"
        f" total={total_ms:.1f}ms"
        f" fetch={fetch_ms:.1f}ms"
    )
    return BusinessDueSummaryResponse(**payload)
