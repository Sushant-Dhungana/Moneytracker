from psycopg import Connection
from psycopg import Error as PsycopgError

from app.core.errors import ApiError
from app.repositories.counterparties_repository import (
    create_counterparty_with_opening,
    get_active_profile_id,
    get_counterparty_by_id,
    list_counterparties,
    list_counterparty_positions,
)


def _resolve_profile_id(conn: Connection, user_id: str) -> str:
    profile_id = get_active_profile_id(conn, user_id)
    if not profile_id:
        raise ApiError(
            status_code=400,
            code="missing_active_profile",
            message="No active profile found for counterparties.",
        )
    return profile_id


def fetch_counterparties(conn: Connection, user_id: str) -> list[dict]:
    profile_id = _resolve_profile_id(conn, user_id)
    return list_counterparties(conn, user_id, profile_id)


def create_counterparty(
    conn: Connection,
    *,
    user_id: str,
    name: str,
    relation_type: str,
    opening_balance: float,
    opening_date: str,
    note: str | None,
) -> dict:
    profile_id = _resolve_profile_id(conn, user_id)
    try:
        counterparty_id = create_counterparty_with_opening(
            conn,
            user_id=user_id,
            profile_id=profile_id,
            name=name.strip(),
            relation_type=relation_type,
            opening_balance=opening_balance,
            opening_date=opening_date,
            note=note,
        )
    except PsycopgError as exc:
        raise ApiError(
            status_code=400, code="counterparty_create_failed", message=str(exc).strip()
        ) from exc

    item = get_counterparty_by_id(conn, user_id=user_id, counterparty_id=counterparty_id)
    if not item:
        raise ApiError(
            status_code=500, code="counterparty_read_failed", message="Created counterparty not found."
        )
    return item


def fetch_counterparty_positions(conn: Connection, user_id: str) -> list[dict]:
    profile_id = _resolve_profile_id(conn, user_id)
    rows = list_counterparty_positions(conn, user_id=user_id, profile_id=profile_id)
    return [
        {
            **row,
            "opening_balance": float(row.get("opening_balance") or 0),
            "receivable": float(row.get("receivable") or 0),
            "payable": float(row.get("payable") or 0),
            "net_position": float(row.get("net_position") or 0),
        }
        for row in rows
    ]

