from datetime import date
from uuid import uuid4

from psycopg import Connection
from psycopg import Error as PsycopgError

from app.core.errors import ApiError
from app.repositories.ledger_repository import (
    create_loan_in_entry,
    create_loan_out_entry,
    create_income_expense_entry,
    create_repayment_in_entry,
    create_repayment_out_entry,
    create_transfer_entry,
    find_ledger_entry_by_transaction_id,
    find_income_expense_entry_by_transaction_id,
    get_active_profile_id,
    get_entry_created_at,
    list_account_ledger_rows,
    list_ledger_postings,
    reverse_income_expense_entry,
    update_income_expense_entry,
)
from app.schemas.ledger import (
    IncomeExpenseCreateRequest,
    IncomeExpenseCreateResponse,
    IncomeExpenseUpdateRequest,
    LedgerEntryIdResponse,
    LoanInCreateRequest,
    LoanOutCreateRequest,
    RepaymentInCreateRequest,
    RepaymentOutCreateRequest,
    TransferCreateRequest,
)


def _validate_not_future(date_value: str) -> None:
    try:
        parsed = date.fromisoformat(date_value)
    except ValueError as exc:
        raise ApiError(status_code=400, code="invalid_date", message="Date must be YYYY-MM-DD.") from exc

    if parsed > date.today():
        raise ApiError(
            status_code=400,
            code="future_date_not_allowed",
            message="Future date is not allowed.",
        )


def create_income_expense(
    conn: Connection, *, user_id: str, payload: IncomeExpenseCreateRequest
) -> IncomeExpenseCreateResponse:
    _validate_not_future(payload.date)

    profile_id = get_active_profile_id(conn, user_id)
    if not profile_id:
        raise ApiError(
            status_code=400,
            code="missing_active_profile",
            message="No active profile found for this user.",
        )

    transaction_id = payload.transaction_id or str(uuid4())
    existing = find_income_expense_entry_by_transaction_id(
        conn, user_id=user_id, transaction_id=transaction_id
    )
    if existing:
        return IncomeExpenseCreateResponse(
            id=transaction_id,
            entry_id=str(existing["ledger_entry_id"]) if existing.get("ledger_entry_id") else None,
            user_id=user_id,
            account_id=str(existing["account_id"]),
            category_id=str(existing["category_id"]) if existing.get("category_id") else None,
            type=str(existing["txn_type"]),
            amount=float(existing["amount"]),
            description=existing.get("description"),
            date=str(existing["date"]),
            created_at=str(existing["created_at"]),
            updated_at=str(existing["created_at"]),
        )

    metadata = {"transaction_id": transaction_id}
    if payload.attachment_url:
        metadata["attachment_url"] = payload.attachment_url

    try:
        entry_id = create_income_expense_entry(
            conn,
            user_id=user_id,
            profile_id=profile_id,
            tx_type=payload.type,
            amount=payload.amount,
            account_id=payload.account_id,
            category_id=payload.category_id,
            date_value=payload.date,
            description=payload.description,
            attachment_url=payload.attachment_url,
            metadata=metadata,
        )
        created_at = get_entry_created_at(conn, user_id=user_id, entry_id=entry_id)
    except PsycopgError as exc:
        raise ApiError(
            status_code=400,
            code="ledger_write_failed",
            message=str(exc).strip() or "Failed to create ledger entry.",
        ) from exc
    except Exception as exc:
        raise ApiError(
            status_code=500,
            code="ledger_write_unhandled",
            message=str(exc).strip() or "Unexpected error while creating transaction.",
        ) from exc

    return IncomeExpenseCreateResponse(
        id=transaction_id,
        entry_id=entry_id,
        user_id=user_id,
        account_id=payload.account_id,
        category_id=payload.category_id,
        type=payload.type,
        amount=float(payload.amount),
        description=payload.description,
        date=payload.date,
        created_at=created_at,
        updated_at=created_at,
    )


def update_income_expense_by_transaction_id(
    conn: Connection,
    *,
    user_id: str,
    transaction_id: str,
    payload: IncomeExpenseUpdateRequest,
) -> IncomeExpenseCreateResponse:
    _validate_not_future(payload.date)
    profile_id = get_active_profile_id(conn, user_id)
    if not profile_id:
        raise ApiError(
            status_code=400,
            code="missing_active_profile",
            message="No active profile found for this user.",
        )

    existing = find_income_expense_entry_by_transaction_id(
        conn, user_id=user_id, transaction_id=transaction_id
    )
    if not existing:
        raise ApiError(status_code=404, code="transaction_not_found", message="Transaction not found.")

    entry_id = existing["ledger_entry_id"]

    try:
        update_income_expense_entry(
            conn,
            user_id=user_id,
            profile_id=profile_id,
            entry_id=entry_id,
            amount=payload.amount,
            date_value=payload.date,
            description=payload.description,
            category_id=payload.category_id,
            account_id=payload.account_id,
            attachment_url=payload.attachment_url,
        )
    except PsycopgError as exc:
        raise ApiError(
            status_code=400,
            code="ledger_update_failed",
            message=str(exc).strip() or "Failed to update transaction.",
        ) from exc
    except Exception as exc:
        raise ApiError(
            status_code=500,
            code="ledger_update_unhandled",
            message=str(exc).strip() or "Unexpected error while updating transaction.",
        ) from exc

    return IncomeExpenseCreateResponse(
        id=transaction_id,
        entry_id=str(entry_id) if entry_id else None,
        user_id=user_id,
        account_id=payload.account_id,
        category_id=payload.category_id,
        type=str(existing["txn_type"]),
        amount=float(payload.amount),
        description=payload.description,
        date=payload.date,
        created_at=str(existing["created_at"]),
        updated_at=str(existing["created_at"]),
    )


def reverse_income_expense_by_transaction_id(
    conn: Connection,
    *,
    user_id: str,
    transaction_id: str,
    reason: str,
) -> None:
    profile_id = get_active_profile_id(conn, user_id)
    if not profile_id:
        raise ApiError(
            status_code=400,
            code="missing_active_profile",
            message="No active profile found for this user.",
        )

    existing = find_income_expense_entry_by_transaction_id(
        conn, user_id=user_id, transaction_id=transaction_id
    )
    if not existing:
        raise ApiError(status_code=404, code="transaction_not_found", message="Transaction not found.")

    entry_id = existing["ledger_entry_id"]
    try:
        reverse_income_expense_entry(
            conn,
            user_id=user_id,
            profile_id=profile_id,
            entry_id=entry_id,
            reason=reason or "User deleted transaction",
        )
    except PsycopgError as exc:
        raise ApiError(
            status_code=400,
            code="ledger_delete_failed",
            message=str(exc).strip() or "Failed to delete transaction.",
        ) from exc
    except Exception as exc:
        raise ApiError(
            status_code=500,
            code="ledger_delete_unhandled",
            message=str(exc).strip() or "Unexpected error while deleting transaction.",
        ) from exc


def _create_ledger_entry_id_response(
    conn: Connection,
    *,
    user_id: str,
    date_value: str,
    txn_type: str,
    transaction_id: str | None,
    create_fn,
) -> LedgerEntryIdResponse:
    _validate_not_future(date_value)

    profile_id = get_active_profile_id(conn, user_id)
    if not profile_id:
        raise ApiError(
            status_code=400,
            code="missing_active_profile",
            message="No active profile found for this user.",
        )

    resolved_transaction_id = transaction_id or str(uuid4())
    existing = find_ledger_entry_by_transaction_id(
        conn,
        user_id=user_id,
        txn_type=txn_type,
        transaction_id=resolved_transaction_id,
    )
    if existing:
        return LedgerEntryIdResponse(entry_id=str(existing["entry_id"]))

    try:
        entry_id = create_fn(profile_id)
    except PsycopgError as exc:
        raise ApiError(
            status_code=400,
            code="ledger_write_failed",
            message=str(exc).strip() or "Failed to create ledger entry.",
        ) from exc
    except Exception as exc:
        raise ApiError(
            status_code=500,
            code="ledger_write_unhandled",
            message=str(exc).strip() or "Unexpected error while creating ledger entry.",
        ) from exc

    return LedgerEntryIdResponse(entry_id=entry_id)


def create_transfer(
    conn: Connection, *, user_id: str, payload: TransferCreateRequest
) -> LedgerEntryIdResponse:
    metadata = dict(payload.metadata or {})
    metadata["transaction_id"] = payload.transaction_id or str(uuid4())
    return _create_ledger_entry_id_response(
        conn,
        user_id=user_id,
        date_value=payload.date,
        txn_type="transfer",
        transaction_id=metadata["transaction_id"],
        create_fn=lambda profile_id: create_transfer_entry(
            conn,
            user_id=user_id,
            profile_id=profile_id,
            from_account_id=payload.from_account_id,
            to_account_id=payload.to_account_id,
            amount=payload.amount,
            date_value=payload.date,
            description=payload.description,
            attachment_url=payload.attachment_url,
            metadata=metadata,
        ),
    )


def create_loan_out(
    conn: Connection, *, user_id: str, payload: LoanOutCreateRequest
) -> LedgerEntryIdResponse:
    metadata = dict(payload.metadata or {})
    metadata["transaction_id"] = payload.transaction_id or str(uuid4())
    return _create_ledger_entry_id_response(
        conn,
        user_id=user_id,
        date_value=payload.date,
        txn_type="loan_out",
        transaction_id=metadata["transaction_id"],
        create_fn=lambda profile_id: create_loan_out_entry(
            conn,
            user_id=user_id,
            profile_id=profile_id,
            counterparty_id=payload.counterparty_id,
            from_account_id=payload.from_account_id,
            amount=payload.amount,
            date_value=payload.date,
            description=payload.description,
            attachment_url=payload.attachment_url,
            metadata=metadata,
        ),
    )


def create_loan_in(
    conn: Connection, *, user_id: str, payload: LoanInCreateRequest
) -> LedgerEntryIdResponse:
    metadata = dict(payload.metadata or {})
    metadata["transaction_id"] = payload.transaction_id or str(uuid4())
    return _create_ledger_entry_id_response(
        conn,
        user_id=user_id,
        date_value=payload.date,
        txn_type="loan_in",
        transaction_id=metadata["transaction_id"],
        create_fn=lambda profile_id: create_loan_in_entry(
            conn,
            user_id=user_id,
            profile_id=profile_id,
            counterparty_id=payload.counterparty_id,
            to_account_id=payload.to_account_id,
            amount=payload.amount,
            date_value=payload.date,
            description=payload.description,
            attachment_url=payload.attachment_url,
            metadata=metadata,
        ),
    )


def create_repayment_in(
    conn: Connection, *, user_id: str, payload: RepaymentInCreateRequest
) -> LedgerEntryIdResponse:
    metadata = dict(payload.metadata or {})
    metadata["transaction_id"] = payload.transaction_id or str(uuid4())
    return _create_ledger_entry_id_response(
        conn,
        user_id=user_id,
        date_value=payload.date,
        txn_type="repayment_in",
        transaction_id=metadata["transaction_id"],
        create_fn=lambda profile_id: create_repayment_in_entry(
            conn,
            user_id=user_id,
            profile_id=profile_id,
            counterparty_id=payload.counterparty_id,
            to_account_id=payload.to_account_id,
            amount=payload.amount,
            date_value=payload.date,
            description=payload.description,
            attachment_url=payload.attachment_url,
            metadata=metadata,
        ),
    )


def create_repayment_out(
    conn: Connection, *, user_id: str, payload: RepaymentOutCreateRequest
) -> LedgerEntryIdResponse:
    metadata = dict(payload.metadata or {})
    metadata["transaction_id"] = payload.transaction_id or str(uuid4())
    return _create_ledger_entry_id_response(
        conn,
        user_id=user_id,
        date_value=payload.date,
        txn_type="repayment_out",
        transaction_id=metadata["transaction_id"],
        create_fn=lambda profile_id: create_repayment_out_entry(
            conn,
            user_id=user_id,
            profile_id=profile_id,
            counterparty_id=payload.counterparty_id,
            from_account_id=payload.from_account_id,
            amount=payload.amount,
            date_value=payload.date,
            description=payload.description,
            attachment_url=payload.attachment_url,
            metadata=metadata,
        ),
    )


def fetch_account_ledger(
    conn: Connection, *, user_id: str, account_id: str
) -> list[dict]:
    return list_account_ledger_rows(conn, user_id=user_id, account_id=account_id)


def fetch_ledger_postings(
    conn: Connection,
    *,
    user_id: str,
    entry_id: str | None = None,
    entry_ids: list[str] | None = None,
    leg_type: str | None = None,
    ref_id: str | None = None,
    include_entry: bool = False,
) -> list[dict]:
    return list_ledger_postings(
        conn,
        user_id=user_id,
        entry_id=entry_id,
        entry_ids=entry_ids,
        leg_type=leg_type,
        ref_id=ref_id,
        include_entry=include_entry,
    )
