from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from psycopg import Connection

from app.core.auth import AuthContext, get_auth_context
from app.core.db import apply_db_auth_context, get_db_conn
from app.services.counterparties_service import (
    create_counterparty,
    fetch_counterparties,
    fetch_counterparty_positions,
)

router = APIRouter(prefix="/counterparties", tags=["counterparties"])


class CounterpartyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    relation_type: str = Field(pattern="^(friend|family|customer|other)$")
    opening_balance: float = 0
    opening_date: str
    note: str | None = None


@router.get("")
def get_counterparties(
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    apply_db_auth_context(conn, auth.user_id)
    return {"items": fetch_counterparties(conn, auth.user_id)}


@router.post("")
def post_counterparty(
    payload: CounterpartyCreateRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    apply_db_auth_context(conn, auth.user_id)
    item = create_counterparty(
        conn,
        user_id=auth.user_id,
        name=payload.name,
        relation_type=payload.relation_type,
        opening_balance=payload.opening_balance,
        opening_date=payload.opening_date,
        note=payload.note,
    )
    return {"item": item}


@router.get("/positions")
def get_counterparty_positions(
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    apply_db_auth_context(conn, auth.user_id)
    return {"items": fetch_counterparty_positions(conn, auth.user_id)}

