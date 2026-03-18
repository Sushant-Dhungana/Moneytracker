from fastapi import APIRouter, Depends, Query
from psycopg import Connection

from app.core.auth import AuthContext, get_auth_context
from app.core.db import apply_db_auth_context, get_db_conn
from app.schemas.ledger import (
    IncomeExpenseCreateRequest,
    IncomeExpenseCreateResponse,
    IncomeExpenseDeleteRequest,
    IncomeExpenseUpdateRequest,
    LedgerEntryIdResponse,
    LoanInCreateRequest,
    LoanOutCreateRequest,
    RepaymentInCreateRequest,
    RepaymentOutCreateRequest,
    TransferCreateRequest,
)
from app.services.ledger_service import (
    create_loan_in,
    create_loan_out,
    create_repayment_in,
    create_repayment_out,
    create_transfer,
    create_income_expense,
    fetch_account_ledger,
    fetch_ledger_postings,
    reverse_income_expense_by_transaction_id,
    update_income_expense_by_transaction_id,
)

router = APIRouter(prefix="/ledger", tags=["ledger"])


@router.get("/account-ledger")
def get_account_ledger(
    account_id: str = Query(...),
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    apply_db_auth_context(conn, auth.user_id)
    items = fetch_account_ledger(conn, user_id=auth.user_id, account_id=account_id)
    return {"items": items}


@router.get("/postings")
def get_ledger_postings(
    entry_id: str | None = Query(default=None),
    entry_ids: str | None = Query(default=None),
    leg_type: str | None = Query(default=None),
    ref_id: str | None = Query(default=None),
    include_entry: bool = Query(default=False),
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    apply_db_auth_context(conn, auth.user_id)
    parsed_entry_ids = [part for part in (entry_ids or "").split(",") if part.strip()] or None
    items = fetch_ledger_postings(
        conn,
        user_id=auth.user_id,
        entry_id=entry_id,
        entry_ids=parsed_entry_ids,
        leg_type=leg_type,
        ref_id=ref_id,
        include_entry=include_entry,
    )
    return {"items": items}


@router.post("/income-expense", response_model=IncomeExpenseCreateResponse)
def post_income_expense(
    payload: IncomeExpenseCreateRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> IncomeExpenseCreateResponse:
    apply_db_auth_context(conn, auth.user_id)
    return create_income_expense(conn, user_id=auth.user_id, payload=payload)


@router.patch("/income-expense/{transaction_id}", response_model=IncomeExpenseCreateResponse)
def patch_income_expense(
    transaction_id: str,
    payload: IncomeExpenseUpdateRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> IncomeExpenseCreateResponse:
    apply_db_auth_context(conn, auth.user_id)
    return update_income_expense_by_transaction_id(
        conn,
        user_id=auth.user_id,
        transaction_id=transaction_id,
        payload=payload,
    )


@router.delete("/income-expense/{transaction_id}")
def delete_income_expense(
    transaction_id: str,
    payload: IncomeExpenseDeleteRequest | None = None,
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    apply_db_auth_context(conn, auth.user_id)
    reverse_income_expense_by_transaction_id(
        conn,
        user_id=auth.user_id,
        transaction_id=transaction_id,
        reason=(payload.reason if payload else None) or "User deleted transaction",
    )
    return {"ok": True}


@router.post("/transfer", response_model=LedgerEntryIdResponse)
def post_transfer(
    payload: TransferCreateRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> LedgerEntryIdResponse:
    apply_db_auth_context(conn, auth.user_id)
    return create_transfer(conn, user_id=auth.user_id, payload=payload)


@router.post("/loan-out", response_model=LedgerEntryIdResponse)
def post_loan_out(
    payload: LoanOutCreateRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> LedgerEntryIdResponse:
    apply_db_auth_context(conn, auth.user_id)
    return create_loan_out(conn, user_id=auth.user_id, payload=payload)


@router.post("/loan-in", response_model=LedgerEntryIdResponse)
def post_loan_in(
    payload: LoanInCreateRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> LedgerEntryIdResponse:
    apply_db_auth_context(conn, auth.user_id)
    return create_loan_in(conn, user_id=auth.user_id, payload=payload)


@router.post("/repayment-in", response_model=LedgerEntryIdResponse)
def post_repayment_in(
    payload: RepaymentInCreateRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> LedgerEntryIdResponse:
    apply_db_auth_context(conn, auth.user_id)
    return create_repayment_in(conn, user_id=auth.user_id, payload=payload)


@router.post("/repayment-out", response_model=LedgerEntryIdResponse)
def post_repayment_out(
    payload: RepaymentOutCreateRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> LedgerEntryIdResponse:
    apply_db_auth_context(conn, auth.user_id)
    return create_repayment_out(conn, user_id=auth.user_id, payload=payload)
