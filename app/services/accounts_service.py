from psycopg import Connection

from app.core.errors import ApiError
from app.repositories.accounts_repository import (
    create_account_with_opening,
    get_account_balances,
    get_account_by_id,
    get_active_profile_id,
    list_accounts,
    update_account_name,
    update_bank_account_settings,
)


def fetch_account_balances(conn: Connection, user_id: str, profile_id: str | None) -> list[dict]:
    resolved_profile_id = profile_id or get_active_profile_id(conn, user_id)
    if not resolved_profile_id:
        raise ApiError(
            status_code=400,
            code="missing_active_profile",
            message="No active profile found for account balance query.",
        )
    return get_account_balances(conn, user_id, resolved_profile_id)


def fetch_accounts(conn: Connection, user_id: str, profile_id: str | None) -> list[dict]:
    resolved_profile_id = profile_id or get_active_profile_id(conn, user_id)
    if not resolved_profile_id:
        raise ApiError(
            status_code=400,
            code="missing_active_profile",
            message="No active profile found for accounts query.",
        )
    return list_accounts(conn, user_id, resolved_profile_id)


def create_account(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str | None,
    name: str,
    account_type: str,
    opening_balance: float,
    opening_date: str,
) -> dict:
    resolved_profile_id = profile_id or get_active_profile_id(conn, user_id)
    if not resolved_profile_id:
        raise ApiError(
            status_code=400,
            code="missing_active_profile",
            message="No active profile found for account creation.",
        )
    account_id = create_account_with_opening(
        conn,
        user_id=user_id,
        profile_id=resolved_profile_id,
        name=name.strip(),
        account_type=account_type,
        opening_balance=opening_balance,
        opening_date=opening_date,
    )
    account = get_account_by_id(conn, user_id=user_id, account_id=account_id)
    if not account:
        raise ApiError(status_code=500, code="account_read_failed", message="Created account not found.")
    return account


def rename_account(conn: Connection, *, user_id: str, account_id: str, name: str) -> dict:
    account = update_account_name(conn, user_id=user_id, account_id=account_id, name=name.strip())
    if not account:
        raise ApiError(status_code=404, code="account_not_found", message="Account not found.")
    return account


def save_bank_account_settings(
    conn: Connection,
    *,
    user_id: str,
    account_id: str,
    name: str,
    allow_overdraft: bool,
    overdraft_limit: float,
) -> dict:
    account = update_bank_account_settings(
        conn,
        user_id=user_id,
        account_id=account_id,
        name=name.strip(),
        allow_overdraft=allow_overdraft,
        overdraft_limit=max(0, float(overdraft_limit or 0)),
    )
    if not account:
        raise ApiError(status_code=404, code="account_not_found", message="Bank account not found.")
    return account
