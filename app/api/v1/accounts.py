from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from psycopg import Connection

from app.core.auth import AuthContext, get_auth_context
from app.core.db import apply_db_auth_context, get_db_conn
from app.schemas.common import AccountBalanceItem, AccountBalancesResponse
from app.services.accounts_service import (
    create_account,
    fetch_account_balances,
    fetch_accounts,
    rename_account,
    save_bank_account_settings,
)

router = APIRouter(prefix="/accounts", tags=["accounts"])


class AccountCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    type: str = Field(pattern="^(cash|bank)$")
    opening_balance: float = 0
    opening_date: str
    qr_image_url: str | None = None
    profile_id: str | None = None


class AccountRenameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class BankAccountSettingsRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    allow_overdraft: bool = False
    overdraft_limit: float = 0
    qr_image_url: str | None = None


@router.get("/balances", response_model=AccountBalancesResponse)
def get_balances(
    profile_id: str | None = Query(default=None),
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> AccountBalancesResponse:
    apply_db_auth_context(conn, auth.user_id)
    rows = fetch_account_balances(conn, auth.user_id, profile_id)
    items = [AccountBalanceItem(**row) for row in rows]
    return AccountBalancesResponse(items=items)


@router.get("")
def get_accounts(
    profile_id: str | None = Query(default=None),
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    apply_db_auth_context(conn, auth.user_id)
    rows = fetch_accounts(conn, auth.user_id, profile_id)
    return {"items": rows}


@router.post("")
def post_account(
    payload: AccountCreateRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    apply_db_auth_context(conn, auth.user_id)
    item = create_account(
        conn,
        user_id=auth.user_id,
        profile_id=payload.profile_id,
        name=payload.name,
        account_type=payload.type,
        opening_balance=payload.opening_balance,
        opening_date=payload.opening_date,
        qr_image_url=payload.qr_image_url,
    )
    return {"item": item}


@router.patch("/{account_id}/name")
def patch_account_name(
    account_id: str,
    payload: AccountRenameRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    apply_db_auth_context(conn, auth.user_id)
    item = rename_account(conn, user_id=auth.user_id, account_id=account_id, name=payload.name)
    return {"item": item}


@router.patch("/{account_id}/bank-settings")
def patch_bank_settings(
    account_id: str,
    payload: BankAccountSettingsRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    apply_db_auth_context(conn, auth.user_id)
    item = save_bank_account_settings(
        conn,
        user_id=auth.user_id,
        account_id=account_id,
        name=payload.name,
        allow_overdraft=payload.allow_overdraft,
        overdraft_limit=payload.overdraft_limit,
        qr_image_url=payload.qr_image_url,
    )
    return {"item": item}
