from datetime import date

from fastapi import APIRouter, Depends, Query
from psycopg import Connection

from app.core.auth import AuthContext, get_auth_context
from app.core.db import apply_db_auth_context, get_db_conn
from app.schemas.common import PaginationMeta, TransactionFeedResponse
from app.services.transactions_service import fetch_transaction_feed, fetch_transaction_feed_item

router = APIRouter(prefix="/transactions", tags=["transactions"])


@router.get("/feed", response_model=TransactionFeedResponse)
def get_transaction_feed(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    txn_type: str | None = Query(default=None),
    account_id: str | None = Query(default=None),
    category_id: str | None = Query(default=None),
    counterparty_id: str | None = Query(default=None),
    profile_id: str | None = Query(default=None),
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> TransactionFeedResponse:
    apply_db_auth_context(conn, auth.user_id)
    params = {
        "user_id": auth.user_id,
        "limit": limit,
        "offset": offset,
        "start_date": start_date,
        "end_date": end_date,
        "txn_type": txn_type,
        "account_id": account_id,
        "category_id": category_id,
        "counterparty_id": counterparty_id,
        "profile_id": profile_id,
    }
    items, total = fetch_transaction_feed(conn, params)
    return TransactionFeedResponse(
        items=items,
        pagination=PaginationMeta(total=total, limit=limit, offset=offset),
    )


@router.get("/{transaction_id}")
def get_transaction_item(
    transaction_id: str,
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    apply_db_auth_context(conn, auth.user_id)
    item = fetch_transaction_feed_item(conn, user_id=auth.user_id, transaction_id=transaction_id)
    if not item:
        return {"item": None}
    return {"item": item}
