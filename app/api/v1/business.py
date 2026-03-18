import base64
import re
from datetime import date, datetime
from decimal import Decimal
from time import perf_counter
from uuid import UUID
from urllib.parse import quote
from urllib.request import Request, urlopen

from fastapi import APIRouter, Depends, Query
from fastapi.encoders import jsonable_encoder
from psycopg import Error as PsycopgError
from psycopg.errors import (
    CheckViolation,
    DeadlockDetected,
    LockNotAvailable,
    QueryCanceled,
    UndefinedColumn,
    UndefinedFunction,
    UndefinedTable,
    UniqueViolation,
)
from psycopg.types.json import Jsonb
from psycopg import Connection

from app.core.auth import AuthContext, get_auth_context
from app.core.db import apply_db_auth_context, get_db_conn
from app.core.errors import ApiError
from app.schemas.business import (
    BusinessAccountNamesResponse,
    BusinessAccountsResponse,
    BusinessAccountTransferRequest,
    BusinessAccountTransferResponse,
    BusinessCustomerHistoryResponse,
    BusinessCustomerSearchResponse,
    BusinessPosBootstrapResponse,
    BusinessProductCreateRequest,
    BusinessProductCreateResponse,
    BusinessProductSalesSummaryResponse,
    BusinessProductListItem,
    BusinessSaleItem,
    BusinessSaleRequest,
    BusinessSaleResponse,
    BusinessStockInBatchItem,
    BusinessStockInBatchRequest,
    BusinessStockInBatchResponse,
    BusinessTransactionsFeedResponse,
    BusinessAccountUpdateRequest,
    BusinessRpcRequest,
    BusinessRpcResponse,
)
from app.services.business_read_service import (
    fetch_business_customer_history,
    fetch_business_pos_bootstrap,
    fetch_business_product_sales_summary,
    fetch_business_transactions_feed,
    search_business_customers,
)
from app.services.ai_business_vector_service import enqueue_business_ai_refresh_job

router = APIRouter(prefix="/business", tags=["business"])
_BUSINESS_CATEGORY_DOMAINS = {"product", "customer", "supplier", "income", "expense"}

_BUSINESS_CATEGORY_SEPARATOR_RE = re.compile(r"[_\-/.]+")
_BUSINESS_CATEGORY_WHITESPACE_RE = re.compile(r"\s+")

_ALLOWED_RPC_NAMES = {
    "create_stock_in_entry",
    "create_stock_in_with_product_source",
    "create_stock_in_with_product",
    "create_business_opening_stock_entry",
    "create_stock_adjustment_entry",
    "create_sales_invoice_entry",
    "collect_invoice_receivable_entry",
    "collect_customer_receivable_entry",
    "list_business_units",
    "create_business_unit",
    "replace_business_unit_products",
    "list_business_product_categories",
    "create_business_product_category",
    "list_business_products",
    "create_business_product",
    "update_business_product",
    "delete_business_product",
    "get_business_stock_summary",
    "get_product_last_party_name",
    "list_business_suppliers",
    "create_business_supplier_with_opening",
    "update_business_supplier",
    "deactivate_business_supplier",
    "create_business_account",
    "repay_business_payable",
}

_BUSINESS_AI_WRITE_RPC_NAMES = {
    "create_stock_in_entry",
    "create_stock_in_with_product_source",
    "create_stock_in_with_product",
    "create_business_opening_stock_entry",
    "create_stock_adjustment_entry",
    "create_sales_invoice_entry",
    "collect_invoice_receivable_entry",
    "collect_customer_receivable_entry",
    "create_business_product_category",
    "create_business_product",
    "update_business_product",
    "replace_business_unit_products",
    "delete_business_product",
    "create_business_supplier_with_opening",
    "update_business_supplier",
    "deactivate_business_supplier",
    "create_business_account",
    "repay_business_payable",
}

_LEGACY_RPC_OPTIONAL_PARAMS = {
    # Older DB deployments may still have pre-category/reminder supplier RPC signatures.
    "create_business_supplier_with_opening": {"p_category_id", "p_reminder_date"},
    "update_business_supplier": {"p_category_id", "p_reminder_date"},
    # Older DB deployments may not have explicit paid amount support for purchase stock-in RPCs.
    "create_stock_in_entry": {"p_paid_amount", "p_supplier_id"},
    "create_stock_in_with_product": {"p_paid_amount", "p_supplier_id"},
    "create_stock_in_with_product_source": {"p_paid_amount", "p_supplier_id"},
    # Some deployments have older invoice RPC signatures that only accept required fields.
    "create_sales_invoice_entry": {
        "p_note",
    },
    # Older deployments may not yet support QR URL on account creation.
    "create_business_account": {"p_qr_image_url"},
    # Older deployments may not yet include selling price argument on product create RPC.
    "create_business_product": {"p_selling_price"},
}

_SUPPLIER_DUPLICATE_CONSTRAINTS = {
    "uq_business_suppliers_profile_name_active",
}

_PRODUCT_DUPLICATE_NAME_CONSTRAINTS = {
    "uq_business_products_profile_name_active",
}

_PRODUCT_DUPLICATE_SKU_CONSTRAINTS = {
    "uq_business_products_profile_sku_active",
}

_RPC_JSONB_PARAMS = {
    # Sales line items payload is JSONB in SQL function signature.
    "create_sales_invoice_entry": {"p_items"},
}

_RPC_SCHEMA_CANDIDATES = ("public", "business")

_RPC_NAME_EXISTS_CACHE: dict[str, bool] = {}
_MUTATION_RECEIPTS_TABLE_EXISTS: bool | None = None

_RPC_POSITIONAL_PARAM_ORDER = {
    # Fallback for environments where parameter names may differ from expected named args.
    "create_sales_invoice_entry": [
        "p_user_id",
        "p_profile_id",
        "p_customer_id",
        "p_date",
        "p_items",
        "p_discount",
        "p_tax",
        "p_payment_mode",
        "p_paid_amount",
        "p_account_id",
        "p_note",
    ],
}


def _mutation_receipts_table_exists(conn: Connection) -> bool:
    global _MUTATION_RECEIPTS_TABLE_EXISTS
    if _MUTATION_RECEIPTS_TABLE_EXISTS is not None:
        return _MUTATION_RECEIPTS_TABLE_EXISTS

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                select to_regclass('public.mutation_receipts') is not null as has_table
                """
            )
            row = cur.fetchone() or {}
        _MUTATION_RECEIPTS_TABLE_EXISTS = bool(row.get("has_table"))
    except Exception:
        _MUTATION_RECEIPTS_TABLE_EXISTS = False
    return _MUTATION_RECEIPTS_TABLE_EXISTS


def _load_mutation_receipt(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    mutation_type: str,
    idempotency_key: str | None,
) -> dict | None:
    normalized_key = str(idempotency_key or "").strip()
    if not normalized_key or not _mutation_receipts_table_exists(conn):
        return None

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                select response_json
                from public.mutation_receipts
                where user_id = %(user_id)s::uuid
                  and profile_id = %(profile_id)s::uuid
                  and mutation_type = %(mutation_type)s
                  and idempotency_key = %(idempotency_key)s
                limit 1
                """,
                {
                    "user_id": user_id,
                    "profile_id": profile_id,
                    "mutation_type": mutation_type,
                    "idempotency_key": normalized_key,
                },
            )
            row = cur.fetchone() or {}
        payload = row.get("response_json")
        return payload if isinstance(payload, dict) else None
    except UndefinedTable:
        return None


def _store_mutation_receipt(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    mutation_type: str,
    idempotency_key: str | None,
    response_payload: dict,
) -> None:
    normalized_key = str(idempotency_key or "").strip()
    if not normalized_key or not _mutation_receipts_table_exists(conn):
        return

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into public.mutation_receipts (
                  user_id,
                  profile_id,
                  mutation_type,
                  idempotency_key,
                  response_json
                )
                values (
                  %(user_id)s::uuid,
                  %(profile_id)s::uuid,
                  %(mutation_type)s,
                  %(idempotency_key)s,
                  %(response_json)s::jsonb
                )
                on conflict (user_id, profile_id, mutation_type, idempotency_key)
                do update set response_json = excluded.response_json
                """,
                {
                    "user_id": user_id,
                    "profile_id": profile_id,
                    "mutation_type": mutation_type,
                    "idempotency_key": normalized_key,
                    "response_json": Jsonb(response_payload),
                },
            )
    except UndefinedTable:
        return


def _sync_business_product_selling_price(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    product_id: str,
    selling_price: float,
    allow_zero: bool = False,
) -> bool:
    normalized_product_id = str(product_id or "").strip()
    if not normalized_product_id:
        return False
    normalized_selling_price = round(float(selling_price or 0), 2)
    if normalized_selling_price < 0:
        return False
    if not allow_zero and normalized_selling_price <= 0:
        return False

    primary_relation = _business_products_relation(conn)
    candidate_relations: list[str] = []
    if primary_relation:
        candidate_relations.append(primary_relation)
    if "public.products" not in candidate_relations:
        public_relation = _first_existing_relation(conn, ["public.products"])
        if public_relation:
            candidate_relations.append(public_relation)

    for relation in candidate_relations:
        has_profile_id = _relation_has_column(conn, relation, "profile_id")
        has_selling_price_col = _relation_has_column(conn, relation, "selling_price")
        has_price_col = _relation_has_column(conn, relation, "price")
        has_updated_at_col = _relation_has_column(conn, relation, "updated_at")
        if not has_selling_price_col and not has_price_col:
            continue

        set_clauses = []
        if has_selling_price_col:
            set_clauses.append("selling_price = %(selling_price)s::numeric")
        else:
            set_clauses.append("price = %(selling_price)s::numeric")
        if has_updated_at_col:
            set_clauses.append("updated_at = now()")

        where_clauses = [
            "id = %(product_id)s::uuid",
            "user_id = %(user_id)s::uuid",
        ]
        if has_profile_id:
            where_clauses.append("profile_id = %(profile_id)s::uuid")

        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    update {relation}
                       set {", ".join(set_clauses)}
                     where {" and ".join(where_clauses)}
                    """,
                    {
                        "selling_price": normalized_selling_price,
                        "product_id": normalized_product_id,
                        "user_id": user_id,
                        "profile_id": profile_id,
                    },
                )
                if cur.rowcount and cur.rowcount > 0:
                    return True
        except PsycopgError as exc:
            print(
                "[Business] Failed to sync selling price on relation",
                relation,
                exc,
            )
            continue
    return False


@router.post("/stock-in/batch", response_model=BusinessStockInBatchResponse)
def post_business_stock_in_batch(
    payload: BusinessStockInBatchRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> BusinessStockInBatchResponse:
    apply_db_auth_context(conn, auth.user_id)
    endpoint_started_at = perf_counter()

    existing_receipt = _load_mutation_receipt(
        conn,
        user_id=auth.user_id,
        profile_id=payload.profile_id,
        mutation_type="business.purchase",
        idempotency_key=payload.idempotency_key,
    )
    if existing_receipt:
        return BusinessStockInBatchResponse(**existing_receipt)

    entry_ids: list[str] = []
    product_ids: list[str] = []
    total_amount = 0.0
    total_paid_amount = 0.0
    inventory_qty_deltas: dict[str, float] = {}

    strategy_started_at = perf_counter()
    stock_in_strategy = _resolve_stock_in_batch_strategy(conn)
    strategy_resolve_ms = (perf_counter() - strategy_started_at) * 1000

    supplier_started_at = perf_counter()
    supplier_name = _resolve_supplier_name(
        conn,
        user_id=auth.user_id,
        profile_id=payload.profile_id,
        supplier_id=payload.supplier_id,
    )
    supplier_resolve_ms = (perf_counter() - supplier_started_at) * 1000

    planned_account_debit = 0.0
    if payload.payment_mode in {"cash", "bank", "merchant"}:
        for item in payload.items:
            item_total_amount = round(float(item.qty or 0) * float(item.unit_cost or 0), 2)
            if item.paid_amount is None:
                item_paid_amount = item_total_amount
            else:
                item_paid_amount = round(
                    min(item_total_amount, max(0.0, float(item.paid_amount or 0))),
                    2,
                )
            planned_account_debit = round(planned_account_debit + item_paid_amount, 2)

    debit_check_ms = 0.0
    batch_item_rpc_ms = 0.0
    try:
        with conn.transaction():
            if planned_account_debit > 0:
                debit_check_started_at = perf_counter()
                _ensure_business_account_debit_capacity(
                    conn,
                    user_id=auth.user_id,
                    profile_id=payload.profile_id,
                    account_id=str(payload.account_id or ""),
                    amount=planned_account_debit,
                    expected_type=payload.payment_mode,
                    for_update=True,
                )
                debit_check_ms = (perf_counter() - debit_check_started_at) * 1000
            for item in payload.items:
                item_total_amount = round(float(item.qty or 0) * float(item.unit_cost or 0), 2)
                if payload.payment_mode == "credit":
                    item_paid_amount = 0.0
                elif item.paid_amount is None:
                    item_paid_amount = item_total_amount
                else:
                    item_paid_amount = round(
                        min(item_total_amount, max(0.0, float(item.paid_amount or 0))),
                        2,
                    )
                item_rpc_started_at = perf_counter()
                result = _execute_stock_in_batch_item_rpc(
                    conn,
                    user_id=auth.user_id,
                    profile_id=payload.profile_id,
                    supplier_id=payload.supplier_id,
                    supplier_name=supplier_name,
                    payment_mode=payload.payment_mode,
                    account_id=payload.account_id,
                    date_value=payload.date,
                    note=payload.note,
                    item=item,
                    strategy=stock_in_strategy,
                )
                batch_item_rpc_ms += (perf_counter() - item_rpc_started_at) * 1000
                row = result[0] if isinstance(result, list) and result else result
                if isinstance(row, dict):
                    entry_id = str(row.get("entry_id") or "").strip()
                    product_id = str(row.get("product_id") or item.product_id).strip()
                    if entry_id:
                        entry_ids.append(entry_id)
                    if product_id:
                        _sync_business_product_selling_price(
                            conn,
                            user_id=auth.user_id,
                            profile_id=payload.profile_id,
                            product_id=product_id,
                            selling_price=float(item.selling_price or 0),
                        )
                        product_ids.append(product_id)
                        inventory_qty_deltas[product_id] = round(
                            inventory_qty_deltas.get(product_id, 0.0)
                            + float(item.qty or 0),
                            3,
                        )

                total_amount += item_total_amount
                total_paid_amount += item_paid_amount
    except ApiError:
        raise

    _enqueue_business_ai_refresh(
        conn,
        user_id=auth.user_id,
        profile_id=payload.profile_id,
    )

    response = BusinessStockInBatchResponse(
        entry_ids=entry_ids,
        product_ids=product_ids,
        total_amount=round(total_amount, 2),
        total_paid_amount=round(total_paid_amount, 2),
        supplier_id=payload.supplier_id,
        occurred_on=payload.date,
        expense_delta=round(total_amount, 2),
        payable_delta=round(max(0.0, total_amount - total_paid_amount), 2),
        account_delta=round(-max(0.0, total_paid_amount), 2),
        inventory_deltas=[
            {"product_id": product_id, "qty_delta": round(qty_delta, 3)}
            for product_id, qty_delta in inventory_qty_deltas.items()
        ],
    )
    receipt_started_at = perf_counter()
    _store_mutation_receipt(
        conn,
        user_id=auth.user_id,
        profile_id=payload.profile_id,
        mutation_type="business.purchase",
        idempotency_key=payload.idempotency_key,
        response_payload=response.model_dump(),
    )
    receipt_ms = (perf_counter() - receipt_started_at) * 1000
    total_ms = (perf_counter() - endpoint_started_at) * 1000
    print(
        "[Perf] api POST /business/stock-in/batch stages:"
        f" total={total_ms:.1f}ms"
        f" items={len(payload.items)}"
        f" strategy={strategy_resolve_ms:.1f}ms"
        f" supplier={supplier_resolve_ms:.1f}ms"
        f" debit_check={debit_check_ms:.1f}ms"
        f" rpc_items={batch_item_rpc_ms:.1f}ms"
        f" receipt={receipt_ms:.1f}ms"
    )
    return response


@router.post("/sales/post", response_model=BusinessSaleResponse)
def post_business_sale(
    payload: BusinessSaleRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> BusinessSaleResponse:
    apply_db_auth_context(conn, auth.user_id)

    existing_receipt = _load_mutation_receipt(
        conn,
        user_id=auth.user_id,
        profile_id=payload.profile_id,
        mutation_type="business.sale",
        idempotency_key=payload.idempotency_key,
    )
    if existing_receipt:
        return BusinessSaleResponse(**existing_receipt)

    payment_mode = str(payload.payment_mode or "").strip().lower()
    if payment_mode not in {"cash", "bank", "merchant", "credit", "partial"}:
        raise ApiError(
            status_code=400,
            code="invalid_payment_mode",
            message="payment_mode must be one of: cash, bank, merchant, credit, partial.",
        )

    total_before_adjustments = sum(
        round(float(item.qty or 0) * float(item.rate or 0), 2) for item in payload.items
    )
    discount_amount = round(float(payload.discount or 0), 2)
    tax_amount = round(float(payload.tax or 0), 2)
    total_amount = round(max(0.0, total_before_adjustments - discount_amount + tax_amount), 2)
    paid_amount = round(max(0.0, float(payload.paid_amount or 0)), 2)
    if payment_mode in {"cash", "bank", "merchant"}:
        paid_amount = total_amount
    elif payment_mode == "credit":
        paid_amount = 0.0
    else:
        paid_amount = round(min(total_amount, paid_amount), 2)
    due_amount = round(max(0.0, total_amount - paid_amount), 2)

    account_id = str(payload.account_id or "").strip() or None
    if payment_mode in {"cash", "bank", "merchant", "partial"} and not account_id:
        raise ApiError(
            status_code=400,
            code="missing_account",
            message="account_id is required for cash/bank/merchant/partial sales.",
        )
    if payment_mode == "partial":
        if paid_amount <= 0:
            raise ApiError(
                status_code=400,
                code="invalid_partial_paid_amount",
                message="Partial payment requires paid_amount greater than zero.",
            )
        if paid_amount >= total_amount:
            raise ApiError(
                status_code=400,
                code="invalid_partial_paid_amount",
                message="Partial payment requires paid_amount less than invoice total.",
            )

    normalized_entry_date = _parse_iso_date_or_raise(payload.date)
    with conn.transaction():
        try:
            result = _execute_named_rpc(
                conn,
                "create_sales_invoice_entry",
                {
                    "p_user_id": auth.user_id,
                    "p_profile_id": payload.profile_id,
                    "p_customer_id": payload.customer_id,
                    "p_date": payload.date,
                    "p_items": [
                        {
                            "product_id": item.product_id,
                            "qty": item.qty,
                            "rate": item.rate,
                        }
                        for item in payload.items
                    ],
                    "p_discount": payload.discount,
                    "p_tax": payload.tax,
                    "p_payment_mode": payment_mode,
                    "p_paid_amount": paid_amount,
                    "p_account_id": account_id,
                    "p_note": payload.note,
                },
            )
        except ApiError as exc:
            if not _should_fallback_to_direct_sale(exc):
                raise
            try:
                result = _create_sales_invoice_entry_direct(
                    conn,
                    auth_user_id=auth.user_id,
                    profile_id=payload.profile_id,
                    customer_id=payload.customer_id,
                    entry_date=normalized_entry_date,
                    items=payload.items,
                    discount=discount_amount,
                    tax=tax_amount,
                    payment_mode=payment_mode,
                    paid_amount=paid_amount,
                    account_id=account_id,
                    note=payload.note,
                )
            except PsycopgError as db_exc:
                raise _translate_rpc_db_error("create_sales_invoice_entry", db_exc) from db_exc

    invoice_id = str(result or "").strip()
    entry_id = _find_sale_entry_id_by_invoice(
        conn,
        user_id=auth.user_id,
        profile_id=payload.profile_id,
        invoice_id=invoice_id,
    )
    response = BusinessSaleResponse(
        invoice_id=invoice_id,
        entry_id=entry_id,
        product_ids=[str(item.product_id) for item in payload.items],
        total_amount=total_amount,
        paid_amount=paid_amount,
        due_amount=due_amount,
        occurred_on=payload.date,
        account_delta=round(max(0.0, paid_amount), 2),
    )
    _enqueue_business_ai_refresh(
        conn,
        user_id=auth.user_id,
        profile_id=payload.profile_id,
    )
    _store_mutation_receipt(
        conn,
        user_id=auth.user_id,
        profile_id=payload.profile_id,
        mutation_type="business.sale",
        idempotency_key=payload.idempotency_key,
        response_payload=response.model_dump(),
    )
    return response


def _find_sale_entry_id_by_invoice(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    invoice_id: str,
) -> str | None:
    normalized_invoice_id = str(invoice_id or "").strip()
    if not normalized_invoice_id:
        return None

    ledger_entries_relation = _business_ledger_entries_relation(conn)
    if not ledger_entries_relation or not _relation_has_column(conn, ledger_entries_relation, "metadata"):
        return None

    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                select id::text as entry_id
                from {ledger_entries_relation}
                where user_id = %(user_id)s::uuid
                  and profile_id = %(profile_id)s::uuid
                  and txn_type = 'sale'
                  and coalesce(metadata ->> 'invoice_id', '') = %(invoice_id)s
                order by created_at desc nulls last, date desc nulls last
                limit 1
                """,
                {
                    "user_id": user_id,
                    "profile_id": profile_id,
                    "invoice_id": normalized_invoice_id,
                },
            )
            row = cur.fetchone() or {}
        entry_id = str(row.get("entry_id") or "").strip()
        return entry_id or None
    except Exception:
        return None


def _create_sales_invoice_entry_direct(
    conn: Connection,
    *,
    auth_user_id: str,
    profile_id: str,
    customer_id: str,
    entry_date: date,
    items: list[BusinessSaleItem],
    discount: float,
    tax: float,
    payment_mode: str,
    paid_amount: float,
    account_id: str | None,
    note: str | None,
) -> str:
    invoices_relation = _business_invoices_relation(conn)
    invoice_items_relation = _business_invoice_items_relation(conn)
    invoice_payments_relation = _business_invoice_payments_relation(conn)
    customers_relation = _business_customers_relation(conn)
    products_relation = _business_products_relation(conn)
    ledger_entries_relation = _business_ledger_entries_relation(conn)
    ledger_postings_relation = _business_ledger_postings_relation(conn)
    stock_state_relation = _business_product_stock_state_relation(conn)
    inventory_movements_relation = _business_inventory_movements_relation(conn)
    units_relation = _first_existing_relation(conn, ["business.units", "public.units"])
    categories_relation = _first_existing_relation(
        conn, ["public.business_categories", "business.product_categories", "public.product_categories"]
    )

    if (
        not invoices_relation
        or not invoice_items_relation
        or not customers_relation
        or not products_relation
        or not ledger_entries_relation
        or not ledger_postings_relation
    ):
        raise ApiError(
            status_code=500,
            code="business_sales_tables_missing",
            message="Business sales posting tables are missing. Apply the latest Supabase migrations to the active database and retry.",
        )

    if not items:
        raise ApiError(
            status_code=400,
            code="invalid_items",
            message="At least one sale item is required.",
        )

    normalized_customer_id = str(customer_id or "").strip()
    try:
        UUID(normalized_customer_id)
    except Exception as exc:  # noqa: BLE001
        raise ApiError(
            status_code=400,
            code="invalid_customer",
            message="Customer id is invalid.",
        ) from exc

    normalized_account_id = str(account_id or "").strip() or None
    if normalized_account_id:
        try:
            UUID(normalized_account_id)
        except Exception as exc:  # noqa: BLE001
            raise ApiError(
                status_code=400,
                code="invalid_account",
                message="Account id is invalid.",
            ) from exc

    has_assert_active_profile = _function_exists(
        conn, "public.assert_active_business_profile(uuid,uuid)"
    )
    has_assert_profile_owner = _function_exists(
        conn, "public.assert_profile_ownership(uuid,uuid)"
    )
    has_customer_active_col = _relation_has_column(conn, customers_relation, "is_active")
    has_customer_phone_col = _relation_has_column(conn, customers_relation, "phone")
    has_product_active_col = _relation_has_column(conn, products_relation, "is_active")
    has_product_quantity_col = _relation_has_column(conn, products_relation, "quantity")
    has_product_updated_at_col = _relation_has_column(conn, products_relation, "updated_at")
    has_entry_account_col = _relation_has_column(conn, ledger_entries_relation, "account_id")
    has_entry_counterparty_col = _relation_has_column(conn, ledger_entries_relation, "counterparty_id")
    has_entry_metadata_col = _relation_has_column(conn, ledger_entries_relation, "metadata")
    has_payment_account_col = bool(
        invoice_payments_relation and _relation_has_column(conn, invoice_payments_relation, "bank_account_id")
    )
    has_stock_qty_col = bool(
        stock_state_relation and _relation_has_column(conn, stock_state_relation, "qty_on_hand")
    )
    has_stock_total_value_col = bool(
        stock_state_relation and _relation_has_column(conn, stock_state_relation, "total_value")
    )
    has_stock_avg_cost_col = bool(
        stock_state_relation and _relation_has_column(conn, stock_state_relation, "avg_unit_cost")
    )
    has_stock_updated_at_col = bool(
        stock_state_relation and _relation_has_column(conn, stock_state_relation, "updated_at")
    )
    has_inventory_created_at_col = bool(
        inventory_movements_relation and _relation_has_column(conn, inventory_movements_relation, "created_at")
    )

    has_invoice_business_name_col = _relation_has_column(conn, invoices_relation, "business_name_snapshot")
    has_invoice_business_phone_col = _relation_has_column(conn, invoices_relation, "business_phone_snapshot")
    has_invoice_business_address_col = _relation_has_column(conn, invoices_relation, "business_address_snapshot")
    has_invoice_business_pan_col = _relation_has_column(conn, invoices_relation, "business_pan_snapshot")
    has_invoice_customer_name_col = _relation_has_column(conn, invoices_relation, "customer_name_snapshot")
    has_invoice_customer_phone_col = _relation_has_column(conn, invoices_relation, "customer_phone_snapshot")
    has_invoice_payment_summary_col = _relation_has_column(conn, invoices_relation, "payment_summary_snapshot")
    has_invoice_pdf_template_col = _relation_has_column(conn, invoices_relation, "pdf_template_version")

    has_invoice_item_product_name_col = _relation_has_column(conn, invoice_items_relation, "product_name_snapshot")
    has_invoice_item_unit_name_col = _relation_has_column(conn, invoice_items_relation, "unit_name_snapshot")
    has_invoice_item_category_name_col = _relation_has_column(conn, invoice_items_relation, "category_name_snapshot")

    expected_account_type = payment_mode if payment_mode in {"cash", "bank", "merchant"} else None
    if paid_amount > 0:
        if not normalized_account_id:
            raise ApiError(
                status_code=400,
                code="missing_account",
                message="Payment account is required for paid sale amount.",
            )
        _load_business_debit_account(
            conn,
            user_id=auth_user_id,
            profile_id=profile_id,
            account_id=normalized_account_id,
            expected_type=expected_account_type,
            for_update=True,
        )

    apply_inventory_movement_exists = _function_exists(
        conn,
        "public.apply_inventory_movement(uuid,uuid,uuid,date,text,uuid,numeric,numeric,numeric,text)",
    )

    with conn.cursor() as cur:
        if has_assert_active_profile:
            cur.execute(
                "select public.assert_active_business_profile(%(user_id)s::uuid, %(profile_id)s::uuid)",
                {"user_id": auth_user_id, "profile_id": profile_id},
            )
        elif has_assert_profile_owner:
            cur.execute(
                "select public.assert_profile_ownership(%(user_id)s::uuid, %(profile_id)s::uuid)",
                {"user_id": auth_user_id, "profile_id": profile_id},
            )

        customer_active_filter = "and c.is_active = true" if has_customer_active_col else ""
        customer_phone_sql = "c.phone" if has_customer_phone_col else "null::text as phone"
        cur.execute(
            f"""
            select
              c.id::text as id,
              coalesce(c.name, '') as name,
              {customer_phone_sql}
            from {customers_relation} c
            where c.id = %(customer_id)s::uuid
              and c.user_id = %(user_id)s::uuid
              and c.profile_id = %(profile_id)s::uuid
              {customer_active_filter}
            limit 1
            """,
            {
                "customer_id": normalized_customer_id,
                "user_id": auth_user_id,
                "profile_id": profile_id,
            },
        )
        customer_row = cur.fetchone() or {}
        customer_name = str(customer_row.get("name") or "").strip()
        customer_phone = str(customer_row.get("phone") or "").strip() or None
        if not customer_name:
            raise ApiError(
                status_code=400,
                code="invalid_customer",
                message="Selected customer is invalid or inactive.",
            )

        cur.execute(
            """
            select
              coalesce(nullif(trim(p.name), ''), 'Business') as business_name,
              p.phone_number as business_phone,
              p.address as business_address,
              p.pan_number as business_pan
            from public.profiles p
            where p.id = %(profile_id)s::uuid
              and p.user_id = %(user_id)s::uuid
              and p.profile_type = 'business'::public.profile_type_enum
            limit 1
            """,
            {"profile_id": profile_id, "user_id": auth_user_id},
        )
        business_profile_row = cur.fetchone() or {}
        business_name = str(business_profile_row.get("business_name") or "Business").strip() or "Business"
        business_phone = str(business_profile_row.get("business_phone") or "").strip() or None
        business_address = str(business_profile_row.get("business_address") or "").strip() or None
        business_pan = str(business_profile_row.get("business_pan") or "").strip() or None

        prepared_items: list[dict[str, object]] = []
        subtotal_amount = 0.0
        for index, item in enumerate(items):
            product_id = str(item.product_id or "").strip()
            try:
                UUID(product_id)
            except Exception as exc:  # noqa: BLE001
                raise ApiError(
                    status_code=400,
                    code="invalid_product",
                    message=f"Sale item #{index + 1} has invalid product id.",
                ) from exc

            qty = round(float(item.qty or 0), 3)
            rate = round(float(item.rate or 0), 2)
            if qty <= 0:
                raise ApiError(
                    status_code=400,
                    code="invalid_qty",
                    message=f"Sale item #{index + 1} quantity must be greater than zero.",
                )
            if rate < 0:
                raise ApiError(
                    status_code=400,
                    code="invalid_rate",
                    message=f"Sale item #{index + 1} rate cannot be negative.",
                )

            product_active_filter = "and p.is_active = true" if has_product_active_col else ""
            quantity_sql = (
                "coalesce(p.quantity, 0)::numeric as quantity"
                if has_product_quantity_col
                else "0::numeric as quantity"
            )
            cur.execute(
                f"""
                select
                  p.id::text as id,
                  coalesce(p.name, '') as name,
                  coalesce(p.price, 0)::numeric as price,
                  p.unit_id::text as unit_id,
                  p.category_id::text as category_id,
                  {quantity_sql}
                from {products_relation} p
                where p.id = %(product_id)s::uuid
                  and p.user_id = %(user_id)s::uuid
                  and p.profile_id = %(profile_id)s::uuid
                  {product_active_filter}
                for update
                limit 1
                """,
                {
                    "product_id": product_id,
                    "user_id": auth_user_id,
                    "profile_id": profile_id,
                },
            )
            product_row = cur.fetchone() or {}
            resolved_product_id = str(product_row.get("id") or "").strip()
            product_name = str(product_row.get("name") or "").strip() or "Product"
            if not resolved_product_id:
                raise ApiError(
                    status_code=400,
                    code="invalid_product",
                    message=f"Product not found or inactive for sale item #{index + 1}.",
                )

            available_qty = round(float(product_row.get("quantity") or 0), 3)
            if stock_state_relation and has_stock_qty_col:
                cur.execute(
                    f"""
                    select coalesce(qty_on_hand, 0) as qty_on_hand
                    from {stock_state_relation}
                    where product_id = %(product_id)s::uuid
                    for update
                    """,
                    {"product_id": resolved_product_id},
                )
                stock_qty_row = cur.fetchone() or {}
                stock_available_qty = round(float(stock_qty_row.get("qty_on_hand") or 0), 3)
                if (not has_product_quantity_col) or stock_available_qty > available_qty:
                    available_qty = stock_available_qty
            if qty - available_qty > 0.0001:
                raise ApiError(
                    status_code=400,
                    code="insufficient_stock",
                    message=f"Insufficient stock for {product_name}.",
                )

            line_total = round(qty * rate, 2)
            unit_cost = round(float(product_row.get("price") or 0), 2)
            subtotal_amount = round(subtotal_amount + line_total, 2)
            new_qty = round(max(0.0, available_qty - qty), 3)

            unit_name = None
            unit_id = str(product_row.get("unit_id") or "").strip()
            if units_relation and unit_id:
                try:
                    UUID(unit_id)
                except Exception:  # noqa: BLE001
                    unit_id = ""
            if units_relation and unit_id:
                cur.execute(
                    f"""
                    select coalesce(name, '') as name
                    from {units_relation}
                    where id = %(unit_id)s::uuid
                    limit 1
                    """,
                    {"unit_id": unit_id},
                )
                unit_row = cur.fetchone() or {}
                unit_name = str(unit_row.get("name") or "").strip() or None

            category_name = None
            category_id = str(product_row.get("category_id") or "").strip()
            if categories_relation and category_id:
                try:
                    UUID(category_id)
                except Exception:  # noqa: BLE001
                    category_id = ""
            if categories_relation and category_id:
                cur.execute(
                    f"""
                    select coalesce(name, '') as name
                    from {categories_relation}
                    where id = %(category_id)s::uuid
                    limit 1
                    """,
                    {"category_id": category_id},
                )
                category_row = cur.fetchone() or {}
                category_name = str(category_row.get("name") or "").strip() or None

            prepared_items.append(
                {
                    "product_id": resolved_product_id,
                    "product_name": product_name,
                    "qty": qty,
                    "rate": rate,
                    "total": line_total,
                    "unit_cost": unit_cost,
                    "new_qty": new_qty,
                    "unit_name": unit_name,
                    "category_name": category_name,
                }
            )

        discount_amount = round(max(0.0, float(discount or 0)), 2)
        tax_amount = round(max(0.0, float(tax or 0)), 2)
        total_amount = round(max(0.0, subtotal_amount - discount_amount + tax_amount), 2)
        adjusted_paid_amount = round(max(0.0, float(paid_amount or 0)), 2)
        if payment_mode in {"cash", "bank", "merchant"}:
            adjusted_paid_amount = total_amount
        elif payment_mode == "credit":
            adjusted_paid_amount = 0.0
        elif payment_mode == "partial":
            adjusted_paid_amount = round(min(total_amount, adjusted_paid_amount), 2)

        due_amount = round(max(0.0, total_amount - adjusted_paid_amount), 2)
        payment_status = "paid" if due_amount <= 0 else ("partial" if adjusted_paid_amount > 0 else "due")

        invoice_no = ""
        if _function_exists(conn, "public.generate_invoice_no()"):
            cur.execute("select public.generate_invoice_no() as invoice_no")
            invoice_no_row = cur.fetchone() or {}
            invoice_no = str(invoice_no_row.get("invoice_no") or "").strip()
        if not invoice_no:
            invoice_no = f"INV-{int(datetime.utcnow().timestamp() * 1000)}"

        invoice_columns = [
            "user_id",
            "profile_id",
            "invoice_no",
            "customer_id",
            "date",
            "subtotal",
            "discount",
            "tax",
            "total",
            "payment_status",
            "note",
        ]
        invoice_values = [
            "%(user_id)s::uuid",
            "%(profile_id)s::uuid",
            "%(invoice_no)s::text",
            "%(customer_id)s::uuid",
            "%(entry_date)s::date",
            "%(subtotal)s::numeric",
            "%(discount)s::numeric",
            "%(tax)s::numeric",
            "%(total)s::numeric",
            "%(payment_status)s::text",
            "%(note)s::text",
        ]
        invoice_bind: dict[str, object] = {
            "user_id": auth_user_id,
            "profile_id": profile_id,
            "invoice_no": invoice_no,
            "customer_id": normalized_customer_id,
            "entry_date": entry_date.isoformat(),
            "subtotal": subtotal_amount,
            "discount": discount_amount,
            "tax": tax_amount,
            "total": total_amount,
            "payment_status": payment_status,
            "note": str(note or "").strip() or None,
        }
        if has_invoice_business_name_col:
            invoice_columns.append("business_name_snapshot")
            invoice_values.append("%(business_name_snapshot)s::text")
            invoice_bind["business_name_snapshot"] = business_name
        if has_invoice_business_phone_col:
            invoice_columns.append("business_phone_snapshot")
            invoice_values.append("%(business_phone_snapshot)s::text")
            invoice_bind["business_phone_snapshot"] = business_phone
        if has_invoice_business_address_col:
            invoice_columns.append("business_address_snapshot")
            invoice_values.append("%(business_address_snapshot)s::text")
            invoice_bind["business_address_snapshot"] = business_address
        if has_invoice_business_pan_col:
            invoice_columns.append("business_pan_snapshot")
            invoice_values.append("%(business_pan_snapshot)s::text")
            invoice_bind["business_pan_snapshot"] = business_pan
        if has_invoice_customer_name_col:
            invoice_columns.append("customer_name_snapshot")
            invoice_values.append("%(customer_name_snapshot)s::text")
            invoice_bind["customer_name_snapshot"] = customer_name
        if has_invoice_customer_phone_col:
            invoice_columns.append("customer_phone_snapshot")
            invoice_values.append("%(customer_phone_snapshot)s::text")
            invoice_bind["customer_phone_snapshot"] = customer_phone
        if has_invoice_payment_summary_col:
            invoice_columns.append("payment_summary_snapshot")
            invoice_values.append("%(payment_summary_snapshot)s::jsonb")
            invoice_bind["payment_summary_snapshot"] = Jsonb(
                {
                    "payment_mode": payment_mode,
                    "paid_amount": adjusted_paid_amount,
                    "due_amount": due_amount,
                    "status": payment_status,
                }
            )
        if has_invoice_pdf_template_col:
            invoice_columns.append("pdf_template_version")
            invoice_values.append("%(pdf_template_version)s::text")
            invoice_bind["pdf_template_version"] = "invoice_a4_v1"

        cur.execute(
            f"""
            insert into {invoices_relation} ({", ".join(invoice_columns)})
            values ({", ".join(invoice_values)})
            returning id::text as invoice_id
            """,
            invoice_bind,
        )
        invoice_row = cur.fetchone() or {}
        invoice_id = str(invoice_row.get("invoice_id") or "").strip()
        if not invoice_id:
            raise ApiError(
                status_code=500,
                code="invoice_create_failed",
                message="Failed to create sales invoice.",
            )

        for item in prepared_items:
            item_columns = [
                "user_id",
                "profile_id",
                "invoice_id",
                "product_id",
                "qty",
                "rate",
                "total",
            ]
            item_values = [
                "%(user_id)s::uuid",
                "%(profile_id)s::uuid",
                "%(invoice_id)s::uuid",
                "%(product_id)s::uuid",
                "%(qty)s::numeric",
                "%(rate)s::numeric",
                "%(total)s::numeric",
            ]
            item_bind: dict[str, object] = {
                "user_id": auth_user_id,
                "profile_id": profile_id,
                "invoice_id": invoice_id,
                "product_id": str(item["product_id"]),
                "qty": float(item["qty"] or 0),
                "rate": float(item["rate"] or 0),
                "total": float(item["total"] or 0),
            }
            if has_invoice_item_product_name_col:
                item_columns.append("product_name_snapshot")
                item_values.append("%(product_name_snapshot)s::text")
                item_bind["product_name_snapshot"] = item["product_name"]
            if has_invoice_item_unit_name_col:
                item_columns.append("unit_name_snapshot")
                item_values.append("%(unit_name_snapshot)s::text")
                item_bind["unit_name_snapshot"] = item.get("unit_name")
            if has_invoice_item_category_name_col:
                item_columns.append("category_name_snapshot")
                item_values.append("%(category_name_snapshot)s::text")
                item_bind["category_name_snapshot"] = item.get("category_name")

            cur.execute(
                f"""
                insert into {invoice_items_relation} ({", ".join(item_columns)})
                values ({", ".join(item_values)})
                """,
                item_bind,
            )

        if invoice_payments_relation and adjusted_paid_amount > 0:
            payment_columns = [
                "user_id",
                "profile_id",
                "invoice_id",
                "mode",
                "amount",
                "date",
            ]
            payment_values = [
                "%(user_id)s::uuid",
                "%(profile_id)s::uuid",
                "%(invoice_id)s::uuid",
                "%(mode)s::text",
                "%(amount)s::numeric",
                "%(entry_date)s::date",
            ]
            payment_bind: dict[str, object] = {
                "user_id": auth_user_id,
                "profile_id": profile_id,
                "invoice_id": invoice_id,
                "mode": payment_mode,
                "amount": adjusted_paid_amount,
                "entry_date": entry_date.isoformat(),
            }
            if has_payment_account_col:
                payment_columns.append("bank_account_id")
                payment_values.append("%(account_id)s::uuid")
                payment_bind["account_id"] = normalized_account_id

            cur.execute(
                f"""
                insert into {invoice_payments_relation} ({", ".join(payment_columns)})
                values ({", ".join(payment_values)})
                """,
                payment_bind,
            )

        description_parts = [f"Sale invoice {invoice_no}", f"Customer: {customer_name}"]
        normalized_note = str(note or "").strip()
        if normalized_note:
            description_parts.append(f"Note: {normalized_note}")
        description = " | ".join(description_parts)

        entry_columns = [
            "user_id",
            "profile_id",
            "txn_type",
            "amount",
            "date",
            "description",
        ]
        entry_values = [
            "%(user_id)s::uuid",
            "%(profile_id)s::uuid",
            "'sale'",
            "%(amount)s::numeric",
            "%(entry_date)s::date",
            "%(description)s::text",
        ]
        entry_bind: dict[str, object] = {
            "user_id": auth_user_id,
            "profile_id": profile_id,
            "amount": total_amount,
            "entry_date": entry_date.isoformat(),
            "description": description,
        }
        if has_entry_account_col:
            entry_columns.append("account_id")
            entry_values.append("%(account_id)s::uuid")
            entry_bind["account_id"] = normalized_account_id
        if has_entry_counterparty_col:
            entry_columns.append("counterparty_id")
            entry_values.append("%(customer_id)s::uuid")
            entry_bind["customer_id"] = normalized_customer_id
        if has_entry_metadata_col:
            entry_columns.append("metadata")
            entry_values.append("%(metadata)s::jsonb")
            entry_bind["metadata"] = Jsonb(
                {
                    "operation": "sale",
                    "source": "business_transactions",
                    "invoice_id": invoice_id,
                    "invoice_no": invoice_no,
                    "customer_id": normalized_customer_id,
                    "customer_name": customer_name,
                    "payment_mode": payment_mode,
                    "paid_amount": adjusted_paid_amount,
                    "due_amount": due_amount,
                }
            )

        cur.execute(
            f"""
            insert into {ledger_entries_relation} ({", ".join(entry_columns)})
            values ({", ".join(entry_values)})
            returning id::text as entry_id
            """,
            entry_bind,
        )
        entry_row = cur.fetchone() or {}
        entry_id = str(entry_row.get("entry_id") or "").strip()
        if not entry_id:
            raise ApiError(
                status_code=500,
                code="sale_entry_insert_failed",
                message="Failed to create sale ledger entry.",
            )

        if adjusted_paid_amount > 0:
            cur.execute(
                f"""
                insert into {ledger_postings_relation} (
                  entry_id, user_id, profile_id, leg_type, ref_id, direction, amount
                )
                values (
                  %(entry_id)s::uuid,
                  %(user_id)s::uuid,
                  %(profile_id)s::uuid,
                  'account',
                  %(account_id)s::uuid,
                  'debit',
                  %(amount)s::numeric
                )
                """,
                {
                    "entry_id": entry_id,
                    "user_id": auth_user_id,
                    "profile_id": profile_id,
                    "account_id": normalized_account_id,
                    "amount": adjusted_paid_amount,
                },
            )

        if due_amount > 0:
            cur.execute(
                f"""
                insert into {ledger_postings_relation} (
                  entry_id, user_id, profile_id, leg_type, ref_id, direction, amount
                )
                values (
                  %(entry_id)s::uuid,
                  %(user_id)s::uuid,
                  %(profile_id)s::uuid,
                  'receivable',
                  %(customer_id)s::uuid,
                  'debit',
                  %(amount)s::numeric
                )
                """,
                {
                    "entry_id": entry_id,
                    "user_id": auth_user_id,
                    "profile_id": profile_id,
                    "customer_id": normalized_customer_id,
                    "amount": due_amount,
                },
            )

        cur.execute(
            f"""
            insert into {ledger_postings_relation} (
              entry_id, user_id, profile_id, leg_type, ref_id, direction, amount
            )
            values (
              %(entry_id)s::uuid,
              %(user_id)s::uuid,
              %(profile_id)s::uuid,
              'sales_revenue',
              null,
              'credit',
              %(amount)s::numeric
            )
            """,
            {
                "entry_id": entry_id,
                "user_id": auth_user_id,
                "profile_id": profile_id,
                "amount": total_amount,
            },
        )

        _assert_business_ledger_entry_is_balanced(
            conn,
            ledger_postings_relation=ledger_postings_relation,
            entry_id=entry_id,
        )

        for item in prepared_items:
            product_id = str(item["product_id"])
            qty = float(item["qty"] or 0)
            unit_cost = float(item["unit_cost"] or 0)
            new_qty = float(item["new_qty"] or 0)

            if apply_inventory_movement_exists:
                cur.execute(
                    """
                    select public.apply_inventory_movement(
                      %(user_id)s::uuid,
                      %(profile_id)s::uuid,
                      %(product_id)s::uuid,
                      %(entry_date)s::date,
                      'sale',
                      %(entry_id)s::uuid,
                      0::numeric,
                      %(qty_out)s::numeric,
                      %(unit_cost)s::numeric,
                      %(note)s::text
                    )
                    """,
                    {
                        "user_id": auth_user_id,
                        "profile_id": profile_id,
                        "product_id": product_id,
                        "entry_date": entry_date.isoformat(),
                        "entry_id": entry_id,
                        "qty_out": qty,
                        "unit_cost": unit_cost,
                        "note": note,
                    },
                )
            else:
                if stock_state_relation and has_stock_qty_col:
                    cur.execute(
                        f"""
                        select
                          coalesce(qty_on_hand, 0) as qty_on_hand,
                          {('coalesce(total_value, 0) as total_value' if has_stock_total_value_col else '0::numeric as total_value')}
                        from {stock_state_relation}
                        where product_id = %(product_id)s::uuid
                        for update
                        """,
                        {"product_id": product_id},
                    )
                    stock_row = cur.fetchone() or {}
                    existing_qty = float(stock_row.get("qty_on_hand") or 0)
                    existing_total = float(stock_row.get("total_value") or 0)
                    next_qty = round(max(0.0, existing_qty - qty), 3)
                    consumed_value = round(qty * unit_cost, 4)
                    next_total = round(max(0.0, existing_total - consumed_value), 4)
                    next_avg = round(next_total / next_qty, 6) if next_qty > 0 else 0.0

                    stock_bind = {
                        "product_id": product_id,
                        "user_id": auth_user_id,
                        "profile_id": profile_id,
                        "qty_on_hand": next_qty,
                        "total_value": next_total,
                        "avg_unit_cost": next_avg,
                    }
                    if stock_row:
                        update_sets: list[str] = []
                        if has_stock_qty_col:
                            update_sets.append("qty_on_hand = %(qty_on_hand)s::numeric")
                        if has_stock_total_value_col:
                            update_sets.append("total_value = %(total_value)s::numeric")
                        if has_stock_avg_cost_col:
                            update_sets.append("avg_unit_cost = %(avg_unit_cost)s::numeric")
                        if has_stock_updated_at_col:
                            update_sets.append("updated_at = now()")
                        if update_sets:
                            cur.execute(
                                f"""
                                update {stock_state_relation}
                                   set {", ".join(update_sets)}
                                 where product_id = %(product_id)s::uuid
                                """,
                                stock_bind,
                            )

                if inventory_movements_relation:
                    movement_columns = [
                        "user_id",
                        "profile_id",
                        "product_id",
                        "date",
                        "ref_type",
                        "ref_id",
                        "qty_in",
                        "qty_out",
                        "unit_cost",
                        "note",
                    ]
                    movement_values = [
                        "%(user_id)s::uuid",
                        "%(profile_id)s::uuid",
                        "%(product_id)s::uuid",
                        "%(entry_date)s::date",
                        "'sale'",
                        "%(entry_id)s::uuid",
                        "0::numeric",
                        "%(qty_out)s::numeric",
                        "%(unit_cost)s::numeric",
                        "%(note)s::text",
                    ]
                    if has_inventory_created_at_col:
                        movement_columns.append("created_at")
                        movement_values.append("now()")
                    cur.execute(
                        f"""
                        insert into {inventory_movements_relation} ({", ".join(movement_columns)})
                        values ({", ".join(movement_values)})
                        """,
                        {
                            "user_id": auth_user_id,
                            "profile_id": profile_id,
                            "product_id": product_id,
                            "entry_date": entry_date.isoformat(),
                            "entry_id": entry_id,
                            "qty_out": qty,
                            "unit_cost": unit_cost,
                            "note": note,
                        },
                    )

            if has_product_quantity_col:
                quantity_update_sql = "quantity = %(new_qty)s::numeric"
                if has_product_updated_at_col:
                    quantity_update_sql += ", updated_at = now()"
                cur.execute(
                    f"""
                    update {products_relation}
                       set {quantity_update_sql}
                     where id = %(product_id)s::uuid
                       and user_id = %(user_id)s::uuid
                       and profile_id = %(profile_id)s::uuid
                    """,
                    {
                        "new_qty": new_qty,
                        "product_id": product_id,
                        "user_id": auth_user_id,
                        "profile_id": profile_id,
                    },
                )

    return invoice_id


def _resolve_supplier_name(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    supplier_id: str | None,
) -> str | None:
    normalized_supplier_id = str(supplier_id or "").strip()
    if not normalized_supplier_id:
        return None

    for relation in ("business.suppliers", "public.suppliers"):
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    select name
                    from {relation}
                    where id = %(supplier_id)s::uuid
                      and user_id = %(user_id)s::uuid
                      and profile_id = %(profile_id)s::uuid
                      and is_active = true
                    limit 1
                    """,
                    {
                        "supplier_id": normalized_supplier_id,
                        "user_id": user_id,
                        "profile_id": profile_id,
                    },
                )
                row = cur.fetchone() or {}
            name = str(row.get("name") or "").strip()
            if name:
                return name
        except UndefinedTable:
            continue

    return None


def _is_missing_rpc_error(exc: ApiError) -> bool:
    return getattr(exc, "code", "") == "business_rpc_missing"


def _should_fallback_to_direct_sale(exc: ApiError) -> bool:
    if _is_missing_rpc_error(exc):
        return True
    code = str(getattr(exc, "code", "") or "").strip().lower()
    if code != "business_rpc_failed":
        return False
    message = str(getattr(exc, "message", "") or "").strip().lower()
    if not message:
        return False
    fallback_markers = (
        "does not exist",
        "undefined function",
        "missing dependency",
        "signature mismatch",
    )
    return any(marker in message for marker in fallback_markers)


def _rpc_name_exists(conn: Connection, rpc_name: str) -> bool:
    cached = _RPC_NAME_EXISTS_CACHE.get(rpc_name)
    if cached is not None:
        return cached
    with conn.cursor() as cur:
        cur.execute(
            """
            select exists (
              select 1
              from pg_proc p
              join pg_namespace n on n.oid = p.pronamespace
              where n.nspname in ('public', 'business')
                and p.proname = %(rpc_name)s
            ) as has_fn
            """,
            {"rpc_name": rpc_name},
        )
        row = cur.fetchone() or {}
    has_fn = bool(row.get("has_fn"))
    _RPC_NAME_EXISTS_CACHE[rpc_name] = has_fn
    return has_fn


def _resolve_stock_in_batch_strategy(conn: Connection) -> str:
    if _rpc_name_exists(conn, "create_stock_in_with_product_source"):
        return "source"
    if _rpc_name_exists(conn, "create_stock_in_with_product"):
        return "with_product"
    if _rpc_name_exists(conn, "create_stock_in_entry"):
        return "entry"
    return "direct"


def _execute_stock_in_batch_item_rpc(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    supplier_id: str | None,
    supplier_name: str | None,
    payment_mode: str,
    account_id: str | None,
    date_value: str,
    note: str | None,
    item: BusinessStockInBatchItem,
    strategy: str | None = None,
) -> object:
    chosen_strategy = strategy or _resolve_stock_in_batch_strategy(conn)
    latest_params = {
        "p_user_id": user_id,
        "p_profile_id": profile_id,
        "p_mode": "existing",
        "p_product_id": item.product_id,
        "p_product_name": None,
        "p_category_id": item.category_id,
        "p_sku": item.sku,
        "p_party_name": supplier_name,
        "p_supplier_id": supplier_id,
        "p_qty": item.qty,
        "p_unit_cost": item.unit_cost,
        "p_selling_price": item.selling_price,
        "p_payment_mode": payment_mode,
        "p_account_id": account_id,
        "p_paid_amount": item.paid_amount,
        "p_date": date_value,
        "p_note": note,
        "p_entry_source": item.entry_source or "stock_in",
    }
    if chosen_strategy == "source":
        try:
            return _execute_named_rpc(conn, "create_stock_in_with_product_source", latest_params)
        except ApiError as exc:
            if not _is_missing_rpc_error(exc):
                raise
            chosen_strategy = "with_product"

    with_product_params = {
        "p_user_id": user_id,
        "p_profile_id": profile_id,
        "p_mode": "existing",
        "p_product_id": item.product_id,
        "p_product_name": None,
        "p_category_id": item.category_id,
        "p_sku": item.sku,
        "p_qty": item.qty,
        "p_unit_cost": item.unit_cost,
        "p_selling_price": item.selling_price,
        "p_payment_mode": payment_mode,
        "p_account_id": account_id,
        "p_date": date_value,
        "p_note": note,
        "p_party_name": supplier_name,
        "p_supplier_id": supplier_id,
        "p_paid_amount": item.paid_amount,
    }
    if chosen_strategy == "with_product":
        try:
            return _execute_named_rpc(conn, "create_stock_in_with_product", with_product_params)
        except ApiError as exc:
            if not _is_missing_rpc_error(exc):
                raise
            chosen_strategy = "entry"

    entry_params = {
        "p_user_id": user_id,
        "p_profile_id": profile_id,
        "p_product_id": item.product_id,
        "p_qty": item.qty,
        "p_unit_cost": item.unit_cost,
        "p_payment_mode": payment_mode,
        "p_account_id": account_id,
        "p_date": date_value,
        "p_note": note,
        "p_party_name": supplier_name,
        "p_supplier_id": supplier_id,
        "p_paid_amount": item.paid_amount,
    }
    if chosen_strategy == "entry":
        try:
            return _execute_named_rpc(conn, "create_stock_in_entry", entry_params)
        except ApiError as exc:
            if not _is_missing_rpc_error(exc):
                raise
            chosen_strategy = "direct"

    return _create_stock_in_batch_item_direct(
        conn,
        user_id=user_id,
        profile_id=profile_id,
        supplier_id=supplier_id,
        supplier_name=supplier_name,
        payment_mode=payment_mode,
        account_id=account_id,
        date_value=date_value,
        note=note,
        item=item,
    )


def _create_stock_in_batch_item_direct(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    supplier_id: str | None,
    supplier_name: str | None,
    payment_mode: str,
    account_id: str | None,
    date_value: str,
    note: str | None,
    item: BusinessStockInBatchItem,
) -> dict:
    products_relation = _business_products_relation(conn)
    ledger_entries_relation = _business_ledger_entries_relation(conn)
    ledger_postings_relation = _business_ledger_postings_relation(conn)
    accounts_relation = _business_accounts_relation(conn)
    suppliers_relation = _business_suppliers_relation(conn)
    stock_state_relation = _business_product_stock_state_relation(conn)
    inventory_movements_relation = _business_inventory_movements_relation(conn)

    if not products_relation or not ledger_entries_relation or not ledger_postings_relation:
        raise ApiError(
            status_code=500,
            code="business_stock_tables_missing",
            message="Business stock posting tables are missing. Apply the latest Supabase migrations to the active database and retry.",
        )

    has_assert_active_profile = _function_exists(
        conn,
        "public.assert_active_business_profile(uuid,uuid)",
    )
    has_assert_profile_owner = _function_exists(
        conn,
        "public.assert_profile_ownership(uuid,uuid)",
    )
    has_product_active_col = _relation_has_column(conn, products_relation, "is_active")
    has_product_quantity_col = _relation_has_column(conn, products_relation, "quantity")
    has_product_updated_at_col = _relation_has_column(conn, products_relation, "updated_at")
    has_entry_account_col = _relation_has_column(conn, ledger_entries_relation, "account_id")
    has_entry_metadata_col = _relation_has_column(conn, ledger_entries_relation, "metadata")
    has_stock_qty_col = bool(
        stock_state_relation and _relation_has_column(conn, stock_state_relation, "qty_on_hand")
    )
    has_stock_total_value_col = bool(
        stock_state_relation and _relation_has_column(conn, stock_state_relation, "total_value")
    )
    has_stock_avg_cost_col = bool(
        stock_state_relation and _relation_has_column(conn, stock_state_relation, "avg_unit_cost")
    )
    has_stock_updated_at_col = bool(
        stock_state_relation and _relation_has_column(conn, stock_state_relation, "updated_at")
    )
    has_inventory_entry_source_col = bool(
        inventory_movements_relation
        and _relation_has_column(conn, inventory_movements_relation, "entry_source")
    )
    has_inventory_created_at_col = bool(
        inventory_movements_relation
        and _relation_has_column(conn, inventory_movements_relation, "created_at")
    )

    normalized_mode = str(payment_mode or "cash").strip().lower()
    if normalized_mode not in {"cash", "bank", "merchant", "credit"}:
        raise ApiError(
            status_code=400,
            code="invalid_payment_mode",
            message="payment_mode must be one of: cash, bank, merchant, credit",
        )

    qty = round(float(item.qty or 0), 3)
    unit_cost = round(float(item.unit_cost or 0), 2)
    if qty <= 0 or unit_cost < 0:
        raise ApiError(
            status_code=400,
            code="invalid_stock_amount",
            message="Quantity must be greater than zero and unit cost must be zero or greater.",
        )

    total_amount = round(qty * unit_cost, 2)
    requested_paid = item.paid_amount
    paid_amount = 0.0
    if requested_paid is not None:
        paid_amount = round(max(0.0, float(requested_paid or 0)), 2)

    if normalized_mode == "credit":
        account_applied = 0.0
        payable_amount = total_amount
    else:
        account_applied = total_amount if requested_paid is None else min(total_amount, paid_amount)
        payable_amount = round(max(0.0, total_amount - account_applied), 2)

    with conn.cursor() as cur:
        if has_assert_active_profile:
            cur.execute(
                "select public.assert_active_business_profile(%(user_id)s::uuid, %(profile_id)s::uuid)",
                {"user_id": user_id, "profile_id": profile_id},
            )
        elif has_assert_profile_owner:
            cur.execute(
                "select public.assert_profile_ownership(%(user_id)s::uuid, %(profile_id)s::uuid)",
                {"user_id": user_id, "profile_id": profile_id},
            )

        active_filter = "and is_active = true" if has_product_active_col else ""
        cur.execute(
            f"""
            select id::text as id, coalesce(name, '') as name
            from {products_relation}
            where id = %(product_id)s::uuid
              and user_id = %(user_id)s::uuid
              and profile_id = %(profile_id)s::uuid
              {active_filter}
            limit 1
            """,
            {
                "product_id": item.product_id,
                "user_id": user_id,
                "profile_id": profile_id,
            },
        )
        product_row = cur.fetchone() or {}
        product_id = str(product_row.get("id") or "").strip()
        product_name = str(product_row.get("name") or "").strip()
        if not product_id:
            raise ApiError(
                status_code=400,
                code="invalid_product",
                message="Selected product is invalid or inactive.",
            )

        resolved_supplier_name = supplier_name
        if supplier_id and suppliers_relation:
            cur.execute(
                f"""
                select name
                from {suppliers_relation}
                where id = %(supplier_id)s::uuid
                  and user_id = %(user_id)s::uuid
                  and profile_id = %(profile_id)s::uuid
                  and is_active = true
                limit 1
                """,
                {
                    "supplier_id": supplier_id,
                    "user_id": user_id,
                    "profile_id": profile_id,
                },
            )
            supplier_row = cur.fetchone() or {}
            candidate_supplier_name = str(supplier_row.get("name") or "").strip()
            if not candidate_supplier_name:
                raise ApiError(
                    status_code=400,
                    code="invalid_supplier",
                    message="Selected supplier is invalid or inactive.",
                )
            resolved_supplier_name = candidate_supplier_name

        if normalized_mode in {"cash", "bank", "merchant"}:
            normalized_account_id = str(account_id or "").strip()
            if not normalized_account_id:
                raise ApiError(
                    status_code=400,
                    code="missing_account",
                    message="Account is required for cash/bank/merchant stock-in.",
                )
            if accounts_relation:
                cur.execute(
                    f"""
                    select 1
                    from {accounts_relation}
                    where id = %(account_id)s::uuid
                      and user_id = %(user_id)s::uuid
                      and profile_id = %(profile_id)s::uuid
                      and is_active = true
                      and type = %(account_type)s
                    limit 1
                    """,
                    {
                        "account_id": normalized_account_id,
                        "user_id": user_id,
                        "profile_id": profile_id,
                        "account_type": normalized_mode,
                    },
                )
                if not cur.fetchone():
                    raise ApiError(
                        status_code=400,
                        code="invalid_account",
                        message=f"Invalid {normalized_mode} account for this profile.",
                    )

        description_parts = []
        normalized_note = str(note or "").strip()
        if normalized_note:
            description_parts.append(normalized_note)
        else:
            description_parts.append("Stock in")
        if resolved_supplier_name:
            description_parts.append(f"Supplier: {resolved_supplier_name}")
        description = " | ".join(description_parts)

        metadata = {
            "product_id": product_id,
            "product_name": product_name,
            "qty": qty,
            "unit_cost": unit_cost,
            "payment_mode": normalized_mode,
            "entry_source": item.entry_source or "stock_in",
            "supplier_id": supplier_id,
            "supplier_name": resolved_supplier_name,
            "paid_amount": round(account_applied, 2),
            "due_amount": round(payable_amount, 2),
        }

        entry_columns = [
            "user_id",
            "profile_id",
            "txn_type",
            "amount",
            "date",
            "description",
        ]
        entry_values = [
            "%(user_id)s::uuid",
            "%(profile_id)s::uuid",
            "'inventory_in'",
            "%(amount)s::numeric",
            "%(entry_date)s::date",
            "%(description)s::text",
        ]
        entry_bind: dict[str, object] = {
            "user_id": user_id,
            "profile_id": profile_id,
            "amount": total_amount,
            "entry_date": date_value,
            "description": description,
        }
        if has_entry_account_col:
            entry_columns.append("account_id")
            entry_values.append("%(account_id)s::uuid")
            entry_bind["account_id"] = account_id
        if has_entry_metadata_col:
            entry_columns.append("metadata")
            entry_values.append("%(metadata)s::jsonb")
            entry_bind["metadata"] = Jsonb(metadata)

        cur.execute(
            f"""
            insert into {ledger_entries_relation} ({", ".join(entry_columns)})
            values ({", ".join(entry_values)})
            returning id::text as entry_id
            """,
            entry_bind,
        )
        entry_row = cur.fetchone() or {}
        entry_id = str(entry_row.get("entry_id") or "").strip()
        if not entry_id:
            raise ApiError(
                status_code=500,
                code="stock_in_insert_failed",
                message="Failed to create stock-in ledger entry.",
            )

        cur.execute(
            f"""
            insert into {ledger_postings_relation} (
              entry_id, user_id, profile_id, leg_type, ref_id, direction, amount
            )
            values (
              %(entry_id)s::uuid,
              %(user_id)s::uuid,
              %(profile_id)s::uuid,
              'inventory_asset',
              %(product_id)s::uuid,
              'debit',
              %(amount)s::numeric
            )
            """,
            {
                "entry_id": entry_id,
                "user_id": user_id,
                "profile_id": profile_id,
                "product_id": product_id,
                "amount": total_amount,
            },
        )

        if account_applied > 0:
            cur.execute(
                f"""
                insert into {ledger_postings_relation} (
                  entry_id, user_id, profile_id, leg_type, ref_id, direction, amount
                )
                values (
                  %(entry_id)s::uuid,
                  %(user_id)s::uuid,
                  %(profile_id)s::uuid,
                  'account',
                  %(account_id)s::uuid,
                  'credit',
                  %(amount)s::numeric
                )
                """,
                {
                    "entry_id": entry_id,
                    "user_id": user_id,
                    "profile_id": profile_id,
                    "account_id": account_id,
                    "amount": round(account_applied, 2),
                },
            )

        if payable_amount > 0:
            cur.execute(
                f"""
                insert into {ledger_postings_relation} (
                  entry_id, user_id, profile_id, leg_type, ref_id, direction, amount
                )
                values (
                  %(entry_id)s::uuid,
                  %(user_id)s::uuid,
                  %(profile_id)s::uuid,
                  'payable',
                  %(supplier_id)s::uuid,
                  'credit',
                  %(amount)s::numeric
                )
                """,
                {
                    "entry_id": entry_id,
                    "user_id": user_id,
                    "profile_id": profile_id,
                    "supplier_id": supplier_id,
                    "amount": round(payable_amount, 2),
                },
            )

        _assert_business_ledger_entry_is_balanced(
            conn,
            ledger_postings_relation=ledger_postings_relation,
            entry_id=entry_id,
        )

        if _function_exists(
            conn,
            "public.apply_inventory_movement(uuid,uuid,uuid,date,text,uuid,numeric,numeric,numeric,text)",
        ):
            cur.execute(
                """
                select public.apply_inventory_movement(
                  %(user_id)s::uuid,
                  %(profile_id)s::uuid,
                  %(product_id)s::uuid,
                  %(entry_date)s::date,
                  'stock_in',
                  %(entry_id)s::uuid,
                  %(qty)s::numeric,
                  0::numeric,
                  %(unit_cost)s::numeric,
                  %(note)s::text
                )
                """,
                {
                    "user_id": user_id,
                    "profile_id": profile_id,
                    "product_id": product_id,
                    "entry_date": date_value,
                    "entry_id": entry_id,
                    "qty": qty,
                    "unit_cost": unit_cost,
                    "note": note,
                },
            )
        else:
            new_qty = qty
            new_total_value = total_amount
            if stock_state_relation and has_stock_qty_col:
                cur.execute(
                    f"""
                    select
                      coalesce(qty_on_hand, 0) as qty_on_hand,
                      {('coalesce(total_value, 0) as total_value' if has_stock_total_value_col else '0::numeric as total_value')}
                    from {stock_state_relation}
                    where product_id = %(product_id)s::uuid
                    for update
                    """,
                    {"product_id": product_id},
                )
                stock_row = cur.fetchone() or {}
                existing_qty = float(stock_row.get("qty_on_hand") or 0)
                existing_total = float(stock_row.get("total_value") or 0)
                new_qty = round(existing_qty + qty, 3)
                new_total_value = round(existing_total + total_amount, 4)
                new_avg_cost = round(new_total_value / new_qty, 6) if new_qty > 0 else 0.0

                stock_bind = {
                    "product_id": product_id,
                    "user_id": user_id,
                    "profile_id": profile_id,
                    "qty_on_hand": new_qty,
                    "total_value": new_total_value,
                    "avg_unit_cost": new_avg_cost,
                }
                if stock_row:
                    update_sets = []
                    if has_stock_qty_col:
                        update_sets.append("qty_on_hand = %(qty_on_hand)s::numeric")
                    if has_stock_total_value_col:
                        update_sets.append("total_value = %(total_value)s::numeric")
                    if has_stock_avg_cost_col:
                        update_sets.append("avg_unit_cost = %(avg_unit_cost)s::numeric")
                    if has_stock_updated_at_col:
                        update_sets.append("updated_at = now()")
                    if update_sets:
                        cur.execute(
                            f"""
                            update {stock_state_relation}
                               set {", ".join(update_sets)}
                             where product_id = %(product_id)s::uuid
                            """,
                            stock_bind,
                        )
                else:
                    insert_columns = ["product_id", "user_id", "profile_id"]
                    insert_values = [
                        "%(product_id)s::uuid",
                        "%(user_id)s::uuid",
                        "%(profile_id)s::uuid",
                    ]
                    if has_stock_qty_col:
                        insert_columns.append("qty_on_hand")
                        insert_values.append("%(qty_on_hand)s::numeric")
                    if has_stock_total_value_col:
                        insert_columns.append("total_value")
                        insert_values.append("%(total_value)s::numeric")
                    if has_stock_avg_cost_col:
                        insert_columns.append("avg_unit_cost")
                        insert_values.append("%(avg_unit_cost)s::numeric")
                    if has_stock_updated_at_col:
                        insert_columns.append("updated_at")
                        insert_values.append("now()")
                    cur.execute(
                        f"""
                        insert into {stock_state_relation} ({", ".join(insert_columns)})
                        values ({", ".join(insert_values)})
                        """,
                        stock_bind,
                    )

            if inventory_movements_relation:
                movement_columns = [
                    "user_id",
                    "profile_id",
                    "product_id",
                    "date",
                    "ref_type",
                    "ref_id",
                    "qty_in",
                    "qty_out",
                    "unit_cost",
                    "note",
                ]
                movement_values = [
                    "%(user_id)s::uuid",
                    "%(profile_id)s::uuid",
                    "%(product_id)s::uuid",
                    "%(entry_date)s::date",
                    "'stock_in'",
                    "%(entry_id)s::uuid",
                    "%(qty)s::numeric",
                    "0::numeric",
                    "%(unit_cost)s::numeric",
                    "%(note)s::text",
                ]
                if has_inventory_entry_source_col:
                    movement_columns.append("entry_source")
                    movement_values.append("%(entry_source)s::text")
                if has_inventory_created_at_col:
                    movement_columns.append("created_at")
                    movement_values.append("now()")
                cur.execute(
                    f"""
                    insert into {inventory_movements_relation} ({", ".join(movement_columns)})
                    values ({", ".join(movement_values)})
                    """,
                    {
                        "user_id": user_id,
                        "profile_id": profile_id,
                        "product_id": product_id,
                        "entry_date": date_value,
                        "entry_id": entry_id,
                        "qty": qty,
                        "unit_cost": unit_cost,
                        "note": note,
                        "entry_source": item.entry_source or "stock_in",
                    },
                )

            if has_product_quantity_col:
                quantity_update_sql = "quantity = %(new_qty)s::numeric"
                if has_product_updated_at_col:
                    quantity_update_sql += ", updated_at = now()"
                cur.execute(
                    f"""
                    update {products_relation}
                       set {quantity_update_sql}
                     where id = %(product_id)s::uuid
                       and user_id = %(user_id)s::uuid
                       and profile_id = %(profile_id)s::uuid
                    """,
                    {
                        "new_qty": new_qty,
                        "product_id": product_id,
                        "user_id": user_id,
                        "profile_id": profile_id,
                    },
                )

    return {
        "entry_id": entry_id,
        "product_id": product_id,
        "created_new_product": False,
        "applied_qty": qty,
        "unit_cost": unit_cost,
        "unit_total": total_amount,
    }


def _create_business_opening_stock_entry_direct(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    product_id: str,
    qty: float,
    unit_cost: float,
    date_value: str,
    note: str | None,
) -> str:
    products_relation = _business_products_relation(conn)
    ledger_entries_relation = _business_ledger_entries_relation(conn)
    ledger_postings_relation = _business_ledger_postings_relation(conn)
    stock_state_relation = _business_product_stock_state_relation(conn)
    inventory_movements_relation = _business_inventory_movements_relation(conn)

    if not products_relation or not ledger_entries_relation or not ledger_postings_relation:
        raise ApiError(
            status_code=500,
            code="business_opening_stock_tables_missing",
            message="Business opening stock posting tables are missing. Apply the latest Supabase migrations to the active database and retry.",
        )

    has_assert_active_profile = _function_exists(
        conn,
        "public.assert_active_business_profile(uuid,uuid)",
    )
    has_assert_profile_owner = _function_exists(
        conn,
        "public.assert_profile_ownership(uuid,uuid)",
    )
    has_product_active_col = _relation_has_column(conn, products_relation, "is_active")
    has_product_quantity_col = _relation_has_column(conn, products_relation, "quantity")
    has_product_updated_at_col = _relation_has_column(conn, products_relation, "updated_at")
    has_entry_metadata_col = _relation_has_column(conn, ledger_entries_relation, "metadata")
    has_stock_qty_col = bool(
        stock_state_relation and _relation_has_column(conn, stock_state_relation, "qty_on_hand")
    )
    has_stock_total_value_col = bool(
        stock_state_relation and _relation_has_column(conn, stock_state_relation, "total_value")
    )
    has_stock_avg_cost_col = bool(
        stock_state_relation and _relation_has_column(conn, stock_state_relation, "avg_unit_cost")
    )
    has_stock_updated_at_col = bool(
        stock_state_relation and _relation_has_column(conn, stock_state_relation, "updated_at")
    )
    has_inventory_created_at_col = bool(
        inventory_movements_relation
        and _relation_has_column(conn, inventory_movements_relation, "created_at")
    )

    rounded_qty = round(float(qty or 0), 3)
    rounded_unit_cost = round(float(unit_cost or 0), 2)
    if rounded_qty <= 0:
        raise ApiError(
            status_code=400,
            code="invalid_opening_qty",
            message="Opening quantity must be greater than zero.",
        )
    if rounded_unit_cost < 0:
        raise ApiError(
            status_code=400,
            code="invalid_opening_unit_cost",
            message="Opening unit cost must be zero or greater.",
        )

    total_amount = round(rounded_qty * rounded_unit_cost, 2)
    normalized_note = str(note or "").strip() or "Opening stock"

    with conn.cursor() as cur:
        if has_assert_active_profile:
            cur.execute(
                "select public.assert_active_business_profile(%(user_id)s::uuid, %(profile_id)s::uuid)",
                {"user_id": user_id, "profile_id": profile_id},
            )
        elif has_assert_profile_owner:
            cur.execute(
                "select public.assert_profile_ownership(%(user_id)s::uuid, %(profile_id)s::uuid)",
                {"user_id": user_id, "profile_id": profile_id},
            )

        active_filter = "and is_active = true" if has_product_active_col else ""
        cur.execute(
            f"""
            select id::text as id
            from {products_relation}
            where id = %(product_id)s::uuid
              and user_id = %(user_id)s::uuid
              and profile_id = %(profile_id)s::uuid
              {active_filter}
            limit 1
            """,
            {
                "product_id": product_id,
                "user_id": user_id,
                "profile_id": profile_id,
            },
        )
        if not cur.fetchone():
            raise ApiError(
                status_code=400,
                code="invalid_product",
                message="Invalid product for this profile.",
            )

        entry_columns = [
            "user_id",
            "profile_id",
            "txn_type",
            "amount",
            "date",
            "description",
        ]
        entry_values = [
            "%(user_id)s::uuid",
            "%(profile_id)s::uuid",
            "'inventory_opening'",
            "%(amount)s::numeric",
            "%(entry_date)s::date",
            "%(description)s::text",
        ]
        entry_bind: dict[str, object] = {
            "user_id": user_id,
            "profile_id": profile_id,
            "amount": total_amount,
            "entry_date": date_value,
            "description": normalized_note,
        }
        if has_entry_metadata_col:
            entry_columns.append("metadata")
            entry_values.append("%(metadata)s::jsonb")
            entry_bind["metadata"] = Jsonb(
                {
                    "product_id": product_id,
                    "qty": rounded_qty,
                    "unit_cost": rounded_unit_cost,
                }
            )

        cur.execute(
            f"""
            insert into {ledger_entries_relation} ({", ".join(entry_columns)})
            values ({", ".join(entry_values)})
            returning id::text as entry_id
            """,
            entry_bind,
        )
        entry_row = cur.fetchone() or {}
        entry_id = str(entry_row.get("entry_id") or "").strip()
        if not entry_id:
            raise ApiError(
                status_code=500,
                code="opening_stock_insert_failed",
                message="Failed to create opening stock ledger entry.",
            )

        cur.execute(
            f"""
            insert into {ledger_postings_relation} (
              entry_id, user_id, profile_id, leg_type, ref_id, direction, amount
            )
            values
              (
                %(entry_id)s::uuid,
                %(user_id)s::uuid,
                %(profile_id)s::uuid,
                'inventory_asset',
                %(product_id)s::uuid,
                'debit',
                %(amount)s::numeric
              ),
              (
                %(entry_id)s::uuid,
                %(user_id)s::uuid,
                %(profile_id)s::uuid,
                'opening_equity',
                null,
                'credit',
                %(amount)s::numeric
              )
            """,
            {
                "entry_id": entry_id,
                "user_id": user_id,
                "profile_id": profile_id,
                "product_id": product_id,
                "amount": total_amount,
            },
        )

        _assert_business_ledger_entry_is_balanced(
            conn,
            ledger_postings_relation=ledger_postings_relation,
            entry_id=entry_id,
        )

        if _function_exists(
            conn,
            "public.apply_inventory_movement(uuid,uuid,uuid,date,text,uuid,numeric,numeric,numeric,text)",
        ):
            cur.execute(
                """
                select public.apply_inventory_movement(
                  %(user_id)s::uuid,
                  %(profile_id)s::uuid,
                  %(product_id)s::uuid,
                  %(entry_date)s::date,
                  'opening',
                  %(entry_id)s::uuid,
                  %(qty)s::numeric,
                  0::numeric,
                  %(unit_cost)s::numeric,
                  %(note)s::text
                )
                """,
                {
                    "user_id": user_id,
                    "profile_id": profile_id,
                    "product_id": product_id,
                    "entry_date": date_value,
                    "entry_id": entry_id,
                    "qty": rounded_qty,
                    "unit_cost": rounded_unit_cost,
                    "note": note,
                },
            )
        else:
            new_qty = rounded_qty
            new_total_value = total_amount
            if stock_state_relation and has_stock_qty_col:
                cur.execute(
                    f"""
                    select
                      coalesce(qty_on_hand, 0) as qty_on_hand,
                      {('coalesce(total_value, 0) as total_value' if has_stock_total_value_col else '0::numeric as total_value')}
                    from {stock_state_relation}
                    where product_id = %(product_id)s::uuid
                    for update
                    """,
                    {"product_id": product_id},
                )
                stock_row = cur.fetchone() or {}
                existing_qty = float(stock_row.get("qty_on_hand") or 0)
                existing_total = float(stock_row.get("total_value") or 0)
                new_qty = round(existing_qty + rounded_qty, 3)
                new_total_value = round(existing_total + total_amount, 4)
                new_avg_cost = round(new_total_value / new_qty, 6) if new_qty > 0 else 0.0

                stock_bind = {
                    "product_id": product_id,
                    "user_id": user_id,
                    "profile_id": profile_id,
                    "qty_on_hand": new_qty,
                    "total_value": new_total_value,
                    "avg_unit_cost": new_avg_cost,
                }
                if stock_row:
                    update_sets = []
                    if has_stock_qty_col:
                        update_sets.append("qty_on_hand = %(qty_on_hand)s::numeric")
                    if has_stock_total_value_col:
                        update_sets.append("total_value = %(total_value)s::numeric")
                    if has_stock_avg_cost_col:
                        update_sets.append("avg_unit_cost = %(avg_unit_cost)s::numeric")
                    if has_stock_updated_at_col:
                        update_sets.append("updated_at = now()")
                    if update_sets:
                        cur.execute(
                            f"""
                            update {stock_state_relation}
                               set {", ".join(update_sets)}
                             where product_id = %(product_id)s::uuid
                            """,
                            stock_bind,
                        )
                else:
                    insert_columns = ["product_id", "user_id", "profile_id"]
                    insert_values = [
                        "%(product_id)s::uuid",
                        "%(user_id)s::uuid",
                        "%(profile_id)s::uuid",
                    ]
                    if has_stock_qty_col:
                        insert_columns.append("qty_on_hand")
                        insert_values.append("%(qty_on_hand)s::numeric")
                    if has_stock_total_value_col:
                        insert_columns.append("total_value")
                        insert_values.append("%(total_value)s::numeric")
                    if has_stock_avg_cost_col:
                        insert_columns.append("avg_unit_cost")
                        insert_values.append("%(avg_unit_cost)s::numeric")
                    if has_stock_updated_at_col:
                        insert_columns.append("updated_at")
                        insert_values.append("now()")
                    cur.execute(
                        f"""
                        insert into {stock_state_relation} ({", ".join(insert_columns)})
                        values ({", ".join(insert_values)})
                        """,
                        stock_bind,
                    )

            if inventory_movements_relation:
                movement_columns = [
                    "user_id",
                    "profile_id",
                    "product_id",
                    "date",
                    "ref_type",
                    "ref_id",
                    "qty_in",
                    "qty_out",
                    "unit_cost",
                    "note",
                ]
                movement_values = [
                    "%(user_id)s::uuid",
                    "%(profile_id)s::uuid",
                    "%(product_id)s::uuid",
                    "%(entry_date)s::date",
                    "'opening'",
                    "%(entry_id)s::uuid",
                    "%(qty)s::numeric",
                    "0::numeric",
                    "%(unit_cost)s::numeric",
                    "%(note)s::text",
                ]
                if has_inventory_created_at_col:
                    movement_columns.append("created_at")
                    movement_values.append("now()")
                cur.execute(
                    f"""
                    insert into {inventory_movements_relation} ({", ".join(movement_columns)})
                    values ({", ".join(movement_values)})
                    """,
                    {
                        "user_id": user_id,
                        "profile_id": profile_id,
                        "product_id": product_id,
                        "entry_date": date_value,
                        "entry_id": entry_id,
                        "qty": rounded_qty,
                        "unit_cost": rounded_unit_cost,
                        "note": note,
                    },
                )

            if has_product_quantity_col:
                quantity_update_sql = "quantity = %(new_qty)s::numeric"
                if has_product_updated_at_col:
                    quantity_update_sql += ", updated_at = now()"
                cur.execute(
                    f"""
                    update {products_relation}
                       set {quantity_update_sql}
                     where id = %(product_id)s::uuid
                       and user_id = %(user_id)s::uuid
                       and profile_id = %(profile_id)s::uuid
                    """,
                    {
                        "new_qty": new_qty,
                        "product_id": product_id,
                        "user_id": user_id,
                        "profile_id": profile_id,
                    },
                )

    return entry_id


def _enqueue_business_ai_refresh(
    conn: Connection, *, user_id: str, profile_id: str | None
) -> None:
    normalized_profile_id = str(profile_id or "").strip()
    if not normalized_profile_id:
        return
    try:
        enqueue_business_ai_refresh_job(
            conn,
            user_id=user_id,
            profile_id=normalized_profile_id,
            source_kind="full_refresh",
            source_id="*",
        )
    except Exception as exc:  # pragma: no cover - background side-effect
        print(f"[BusinessAI] Failed to enqueue index refresh job: {exc}")

def _to_json_safe(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _to_json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_json_safe(v) for v in value]
    return value


def _run_named_rpc_query(
    conn: Connection, rpc_name: str, params: dict, *, rpc_schema: str = "public"
) -> list[object]:
    assignments = []
    bind = {}
    for key, value in (params or {}).items():
        safe_key = str(key)
        if not safe_key.startswith("p_"):
            raise ApiError(
                status_code=400,
                code="invalid_rpc_param",
                message=f"Invalid RPC parameter: {safe_key}",
            )
        assignments.append(f"{safe_key} := %({safe_key})s")
        if safe_key in _RPC_JSONB_PARAMS.get(rpc_name, set()) and value is not None:
            bind[safe_key] = Jsonb(value)
        else:
            bind[safe_key] = value

    arg_sql = ", ".join(assignments)
    query = f"select {rpc_schema}.{rpc_name}({arg_sql}) as result"

    with conn.cursor() as cur:
        cur.execute(query, bind)
        rows = cur.fetchall() or []
    return rows


def _run_positional_rpc_query(
    conn: Connection,
    rpc_name: str,
    params: dict,
    *,
    rpc_schema: str = "public",
) -> list[object]:
    configured_keys = _RPC_POSITIONAL_PARAM_ORDER.get(rpc_name, [])
    ordered_keys = [key for key in configured_keys if key in (params or {})]
    if not ordered_keys:
        raise UndefinedFunction(f"No positional RPC mapping for '{rpc_name}'")

    bind: dict[str, object] = {}
    placeholders: list[str] = []
    for key in ordered_keys:
        if key in _RPC_JSONB_PARAMS.get(rpc_name, set()) and params.get(key) is not None:
            bind[key] = Jsonb(params.get(key))
        else:
            bind[key] = params.get(key)
        placeholders.append(f"%({key})s")

    arg_sql = ", ".join(placeholders)
    query = f"select {rpc_schema}.{rpc_name}({arg_sql}) as result"
    with conn.cursor() as cur:
        cur.execute(query, bind)
        rows = cur.fetchall() or []
    return rows


def _legacy_rpc_fallback_params(rpc_name: str, params: dict) -> dict | None:
    optional_keys = _LEGACY_RPC_OPTIONAL_PARAMS.get(rpc_name)
    if not optional_keys:
        return None
    next_params = {k: v for k, v in (params or {}).items() if str(k) not in optional_keys}
    if len(next_params) == len(params or {}):
        return None
    return next_params


def _rpc_missing_message(rpc_name: str, exc: Exception) -> str:
    detail = str(exc).strip()
    detail_lower = detail.lower()
    rpc_token = rpc_name.lower()

    # When the target RPC itself is absent, keep the concise migration guidance.
    if rpc_token in detail_lower and "does not exist" in detail_lower:
        return (
            f"Database RPC '{rpc_name}' is missing. "
            "Apply the latest Supabase migrations to the active database and retry."
        )

    # Otherwise surface the underlying dependency/signature issue to make diagnosis actionable.
    return (
        f"Database RPC '{rpc_name}' could not be executed due to a missing dependency/signature mismatch: "
        f"{detail}. Apply the latest Supabase migrations to the active database and retry."
    )


def _translate_rpc_db_error(rpc_name: str, exc: PsycopgError) -> ApiError:
    if isinstance(exc, UniqueViolation):
        constraint_name = ""
        diag = getattr(exc, "diag", None)
        if diag is not None:
            constraint_name = str(getattr(diag, "constraint_name", "") or "")

        if (
            constraint_name in _SUPPLIER_DUPLICATE_CONSTRAINTS
            or "suppliers" in constraint_name
            or rpc_name in {"create_business_supplier_with_opening", "update_business_supplier"}
        ):
            return ApiError(
                status_code=409,
                code="supplier_name_exists",
                message="Supplier with this name already exists in this profile.",
            )

        if constraint_name in _PRODUCT_DUPLICATE_NAME_CONSTRAINTS:
            return ApiError(
                status_code=409,
                code="product_name_exists",
                message="Product name already exists for this business profile. Please use a unique name.",
            )

        if constraint_name in _PRODUCT_DUPLICATE_SKU_CONSTRAINTS:
            return ApiError(
                status_code=409,
                code="product_sku_exists",
                message="SKU already exists for this business profile. Please use a unique SKU.",
            )

        return ApiError(
            status_code=409,
            code="duplicate_record",
            message="A record with the same unique value already exists.",
        )

    message = str(exc).strip() or f"Failed to execute RPC '{rpc_name}'."
    return ApiError(
        status_code=400,
        code="business_rpc_failed",
        message=message,
    )


def _execute_named_rpc(conn: Connection, rpc_name: str, params: dict) -> object:
    if rpc_name not in _ALLOWED_RPC_NAMES:
        raise ApiError(
            status_code=400,
            code="business_rpc_not_allowed",
            message=f"RPC not allowed: {rpc_name}",
        )

    def _execute_with_schema_fallback(call_params: dict) -> list[object]:
        last_exc: UndefinedFunction | None = None
        for rpc_schema in _RPC_SCHEMA_CANDIDATES:
            try:
                # Use a savepoint-scoped transaction block so a signature failure doesn't
                # abort the outer request transaction before fallback retry.
                with conn.transaction():
                    return _run_named_rpc_query(
                        conn, rpc_name, call_params, rpc_schema=rpc_schema
                    )
            except UndefinedFunction as schema_exc:
                last_exc = schema_exc
                if rpc_name in _RPC_POSITIONAL_PARAM_ORDER:
                    try:
                        with conn.transaction():
                            return _run_positional_rpc_query(
                                conn, rpc_name, call_params, rpc_schema=rpc_schema
                            )
                    except UndefinedFunction as positional_exc:
                        last_exc = positional_exc
                        continue
                continue
        if last_exc is not None:
            raise last_exc
        return []

    try:
        try:
            rows = _execute_with_schema_fallback(params)
        except UndefinedFunction as exc:
            fallback_params = _legacy_rpc_fallback_params(rpc_name, params)
            if fallback_params is not None:
                try:
                    rows = _execute_with_schema_fallback(fallback_params)
                except UndefinedFunction as fallback_exc:
                    raise ApiError(
                        status_code=500,
                        code="business_rpc_missing",
                        message=_rpc_missing_message(rpc_name, fallback_exc),
                    ) from fallback_exc
            else:
                raise ApiError(
                    status_code=500,
                    code="business_rpc_missing",
                    message=_rpc_missing_message(rpc_name, exc),
                ) from exc
    except PsycopgError as exc:
        raise _translate_rpc_db_error(rpc_name, exc) from exc

    expects_list = rpc_name.startswith("list_")

    if not rows:
        if expects_list:
            return []
        return None
    if len(rows) == 1:
        row = rows[0]
        if isinstance(row, dict) and "result" in row and len(row) == 1:
            result = row["result"]
            if expects_list:
                return [] if result is None else [result]
            return result
        if expects_list:
            return [row]
        return row
    return [row.get("result", row) if isinstance(row, dict) else row for row in rows]


def _list_business_units_direct(
    conn: Connection,
    *,
    auth_user_id: str,
    profile_id: str,
) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "select public.assert_active_business_profile(%(user_id)s::uuid, %(profile_id)s::uuid)",
            {"user_id": auth_user_id, "profile_id": profile_id},
        )
        cur.execute(
            """
            select
              id::text as id,
              user_id::text as user_id,
              profile_id::text as profile_id,
              name,
              created_at::text as created_at,
              updated_at::text as updated_at
            from business.units
            where user_id = %(user_id)s::uuid
              and profile_id = %(profile_id)s::uuid
            order by name asc
            """,
            {"user_id": auth_user_id, "profile_id": profile_id},
        )
        rows = cur.fetchall() or []
    return rows


def _create_business_unit_direct(
    conn: Connection,
    *,
    auth_user_id: str,
    profile_id: str,
    name: str,
) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "select public.assert_active_business_profile(%(user_id)s::uuid, %(profile_id)s::uuid)",
            {"user_id": auth_user_id, "profile_id": profile_id},
        )
        cur.execute(
            """
            insert into business.units (user_id, profile_id, name)
            values (%(user_id)s::uuid, %(profile_id)s::uuid, %(name)s::text)
            returning
              id::text as id,
              user_id::text as user_id,
              profile_id::text as profile_id,
              name,
              created_at::text as created_at,
              updated_at::text as updated_at
            """,
            {
                "user_id": auth_user_id,
                "profile_id": profile_id,
                "name": name.strip(),
            },
        )
        row = cur.fetchone() or {}
    return row


def _replace_business_unit_products_direct(
    conn: Connection,
    *,
    auth_user_id: str,
    profile_id: str,
    from_unit_id: str,
    to_unit_id: str,
) -> dict:
    normalized_from_unit_id = str(from_unit_id or "").strip()
    normalized_to_unit_id = str(to_unit_id or "").strip()
    if not normalized_from_unit_id or not normalized_to_unit_id:
        raise ApiError(
            status_code=400,
            code="invalid_rpc_param",
            message="Both from_unit_id and to_unit_id are required.",
        )

    try:
        UUID(normalized_from_unit_id)
        UUID(normalized_to_unit_id)
    except Exception as exc:  # noqa: BLE001
        raise ApiError(
            status_code=400,
            code="invalid_unit",
            message="Unit id is invalid.",
        ) from exc

    if normalized_from_unit_id == normalized_to_unit_id:
        return {
            "updated_count": 0,
            "from_unit_id": normalized_from_unit_id,
            "to_unit_id": normalized_to_unit_id,
        }

    units_relation = _first_existing_relation(conn, ["business.units", "public.units"])
    products_relation = _business_products_relation(conn)
    if not products_relation:
        raise ApiError(
            status_code=500,
            code="products_table_missing",
            message="Product table is not available.",
        )

    has_assert_active_profile = _function_exists(
        conn,
        "public.assert_active_business_profile(uuid,uuid)",
    )
    has_assert_profile_owner = _function_exists(
        conn,
        "public.assert_profile_ownership(uuid,uuid)",
    )
    has_product_user_id = _relation_has_column(conn, products_relation, "user_id")
    has_product_profile_id = _relation_has_column(conn, products_relation, "profile_id")
    has_product_unit_id = _relation_has_column(conn, products_relation, "unit_id")
    has_product_updated_at = _relation_has_column(conn, products_relation, "updated_at")

    if not has_product_unit_id:
        raise ApiError(
            status_code=500,
            code="products_unit_column_missing",
            message="Products table does not expose unit_id.",
        )

    with conn.cursor() as cur:
        if has_assert_active_profile:
            cur.execute(
                "select public.assert_active_business_profile(%(user_id)s::uuid, %(profile_id)s::uuid)",
                {"user_id": auth_user_id, "profile_id": profile_id},
            )
        elif has_assert_profile_owner:
            cur.execute(
                "select public.assert_profile_ownership(%(user_id)s::uuid, %(profile_id)s::uuid)",
                {"user_id": auth_user_id, "profile_id": profile_id},
            )

        if units_relation:
            has_units_profile_id = _relation_has_column(conn, units_relation, "profile_id")
            for unit_id in (normalized_from_unit_id, normalized_to_unit_id):
                where_parts = [
                    "id = %(unit_id)s::uuid",
                    "user_id = %(user_id)s::uuid",
                ]
                if has_units_profile_id:
                    where_parts.append("profile_id = %(profile_id)s::uuid")
                cur.execute(
                    f"""
                    select id::text as id
                    from {units_relation}
                    where {" and ".join(where_parts)}
                    limit 1
                    """,
                    {
                        "unit_id": unit_id,
                        "user_id": auth_user_id,
                        "profile_id": profile_id,
                    },
                )
                if not cur.fetchone():
                    raise ApiError(
                        status_code=400,
                        code="invalid_unit",
                        message="Selected unit is invalid for this profile.",
                    )

        set_parts = ["unit_id = %(to_unit_id)s::uuid"]
        if has_product_updated_at:
            set_parts.append("updated_at = now()")

        where_parts = ["unit_id = %(from_unit_id)s::uuid"]
        if has_product_user_id:
            where_parts.append("user_id = %(user_id)s::uuid")
        if has_product_profile_id:
            where_parts.append("profile_id = %(profile_id)s::uuid")

        cur.execute(
            f"""
            update {products_relation}
               set {", ".join(set_parts)}
             where {" and ".join(where_parts)}
            """,
            {
                "to_unit_id": normalized_to_unit_id,
                "from_unit_id": normalized_from_unit_id,
                "user_id": auth_user_id,
                "profile_id": profile_id,
            },
        )
        updated_count = int(cur.rowcount or 0)

    return {
        "updated_count": updated_count,
        "from_unit_id": normalized_from_unit_id,
        "to_unit_id": normalized_to_unit_id,
    }


def _list_business_products_direct(
    conn: Connection,
    *,
    auth_user_id: str,
    profile_id: str,
) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "select public.assert_active_business_profile(%(user_id)s::uuid, %(profile_id)s::uuid)",
            {"user_id": auth_user_id, "profile_id": profile_id},
        )
        cur.execute(
            """
            select *
            from public.list_business_products(%(profile_id)s::uuid)
            """,
            {"profile_id": profile_id},
        )
        rows = cur.fetchall() or []
    return rows


def _ensure_active_business_profile_for_account_create(
    conn: Connection,
    *,
    auth_user_id: str,
    params: dict,
) -> None:
    if "p_profile_id" not in params:
        raise ApiError(
            status_code=400,
            code="invalid_rpc_param",
            message="Missing required RPC parameter: p_profile_id",
        )

    requested_user_id = str(params.get("p_user_id") or "").strip()
    if requested_user_id and requested_user_id != auth_user_id:
        raise ApiError(
            status_code=403,
            code="forbidden_rpc_user",
            message="p_user_id does not match authenticated user.",
        )

    profile_id = str(params["p_profile_id"]).strip()
    with conn.cursor() as cur:
        cur.execute(
            """
            update public.user_profiles up
               set active_profile_id = p.id
              from public.profiles p
             where up.id = %(user_id)s::uuid
               and p.id = %(profile_id)s::uuid
               and p.user_id = %(user_id)s::uuid
               and p.profile_type = 'business'
            returning up.active_profile_id
            """,
            {"user_id": auth_user_id, "profile_id": profile_id},
        )
        row = cur.fetchone()

    if not row:
        raise ApiError(
            status_code=400,
            code="invalid_business_profile",
            message="Provided profile is not an owned business profile.",
        )


def _validate_business_category_domain(domain: str) -> str:
    normalized = str(domain or "").strip().lower()
    if normalized not in _BUSINESS_CATEGORY_DOMAINS:
        raise ApiError(
            status_code=400,
            code="invalid_domain",
            message="domain must be one of: product, customer, supplier, income, expense.",
        )
    return normalized


def _normalize_business_category_display_name(value: str) -> str:
    return _BUSINESS_CATEGORY_WHITESPACE_RE.sub(" ", str(value or "").strip()).strip()


def _business_category_compare_key(value: str) -> str:
    display = _normalize_business_category_display_name(value).lower()
    normalized = _BUSINESS_CATEGORY_SEPARATOR_RE.sub(" ", display)
    return _BUSINESS_CATEGORY_WHITESPACE_RE.sub(" ", normalized).strip()


def _business_category_compact_key(value: str) -> str:
    return _business_category_compare_key(value).replace(" ", "")


def _business_category_edit_distance(source: str, target: str) -> int:
    if source == target:
        return 0
    if not source:
        return len(target)
    if not target:
        return len(source)

    previous = list(range(len(target) + 1))
    current = [0] * (len(target) + 1)

    for row_index, source_char in enumerate(source, start=1):
        current[0] = row_index
        for col_index, target_char in enumerate(target, start=1):
            cost = 0 if source_char == target_char else 1
            current[col_index] = min(
                current[col_index - 1] + 1,
                previous[col_index] + 1,
                previous[col_index - 1] + cost,
            )
        previous, current = current, previous

    return previous[len(target)]


def _get_business_category_strong_match_reason(
    input_compare_key: str,
    input_compact_key: str,
    candidate_compare_key: str,
    candidate_compact_key: str,
) -> str | None:
    if not candidate_compare_key or not candidate_compact_key:
        return None
    if candidate_compare_key == input_compare_key:
        return "exact_normalized"
    if candidate_compact_key == input_compact_key:
        return "compact_normalized"
    if (
        len(input_compact_key) < 5
        or len(candidate_compact_key) < 5
        or input_compact_key[:4] != candidate_compact_key[:4]
        or abs(len(input_compact_key) - len(candidate_compact_key)) > 3
    ):
        return None
    max_length = max(len(input_compact_key), len(candidate_compact_key))
    max_distance = 2 if max_length >= 8 else 1
    edit_distance = _business_category_edit_distance(input_compact_key, candidate_compact_key)
    similarity = 1 - edit_distance / max_length if max_length else 0
    return "fuzzy_strong" if edit_distance <= max_distance or similarity >= 0.7 else None


def _find_strong_matching_business_category(
    items: list[dict],
    *,
    name: str,
) -> tuple[dict | None, str | None]:
    input_compare_key = _business_category_compare_key(name)
    input_compact_key = _business_category_compact_key(name)
    if not input_compare_key or not input_compact_key:
        return None, None

    for item in items:
        candidate_compare_key = _business_category_compare_key(item.get("name") or "")
        candidate_compact_key = _business_category_compact_key(item.get("name") or "")
        reason = _get_business_category_strong_match_reason(
            input_compare_key,
            input_compact_key,
            candidate_compare_key,
            candidate_compact_key,
        )
        if reason:
            return item, reason
    return None, None


def _list_business_category_candidates(
    conn: Connection,
    *,
    auth: AuthContext,
    profile_id: str,
    domain: str,
    parent_id: str | None,
    exclude_category_id: str | None = None,
    relation: str | None = None,
    legacy_has_profile_id: bool | None = None,
    legacy_has_parent_id: bool | None = None,
    legacy_has_type: bool | None = None,
    legacy_has_is_active: bool | None = None,
    legacy_has_updated_at: bool | None = None,
) -> list[dict]:
    if relation:
        has_profile_id = bool(legacy_has_profile_id)
        has_parent_id = bool(legacy_has_parent_id)
        has_type = bool(legacy_has_type)
        has_is_active = bool(legacy_has_is_active)
        has_updated_at = bool(legacy_has_updated_at)

        where_parts = ["user_id = %(user_id)s::uuid"]
        if has_profile_id:
            where_parts.append("profile_id = %(profile_id)s::uuid")
        if has_type:
            where_parts.append("type = %(domain)s::text")
        if has_is_active:
            where_parts.append("is_active = true")
        if has_parent_id:
            if parent_id:
                where_parts.append("parent_id = %(parent_id)s::uuid")
            else:
                where_parts.append("parent_id is null")
        if exclude_category_id:
            where_parts.append("id <> %(exclude_category_id)s::uuid")

        with conn.cursor() as cur:
            cur.execute(
                f"""
                select
                  id,
                  user_id,
                  {'profile_id' if has_profile_id else '%(profile_id)s::uuid as profile_id'},
                  {'type::text' if has_type else '%(domain)s::text'} as domain,
                  name,
                  {'parent_id' if has_parent_id else 'null::uuid'} as parent_id,
                  {'is_active' if has_is_active else 'true'} as is_active,
                  created_at,
                  {'updated_at' if has_updated_at else 'created_at'} as updated_at
                from {relation}
                where {' and '.join(where_parts)}
                order by created_at asc
                """,
                {
                    "user_id": auth.user_id,
                    "profile_id": profile_id,
                    "domain": domain,
                    "parent_id": parent_id,
                    "exclude_category_id": exclude_category_id,
                },
            )
            return cur.fetchall() or []

    with conn.cursor() as cur:
        where_parts = [
            "user_id = %(user_id)s::uuid",
            "profile_id = %(profile_id)s::uuid",
            "domain = %(domain)s::text",
            "is_active = true",
        ]
        if parent_id:
            where_parts.append("parent_id = %(parent_id)s::uuid")
        else:
            where_parts.append("parent_id is null")
        if exclude_category_id:
            where_parts.append("id <> %(exclude_category_id)s::uuid")

        cur.execute(
            f"""
            select
              id, user_id, profile_id, domain, name, parent_id, is_active, created_at, updated_at
            from public.business_categories
            where {' and '.join(where_parts)}
            order by created_at asc
            """,
            {
                "user_id": auth.user_id,
                "profile_id": profile_id,
                "domain": domain,
                "parent_id": parent_id,
                "exclude_category_id": exclude_category_id,
            },
        )
        return cur.fetchall() or []


def _has_unified_business_categories(conn: Connection) -> bool:
    with conn.cursor() as cur:
        cur.execute("select to_regclass('public.business_categories') as rel")
        row = cur.fetchone() or {}
    return bool(row.get("rel"))


def _first_existing_relation(conn: Connection, candidates: list[str]) -> str | None:
    with conn.cursor() as cur:
        for relation in candidates:
            cur.execute("select to_regclass(%(relation)s) as rel", {"relation": relation})
            row = cur.fetchone() or {}
            if row.get("rel"):
                return relation
    return None


def _relation_has_column(conn: Connection, relation: str, column_name: str) -> bool:
    if "." not in relation:
        return False
    schema_name, table_name = relation.split(".", 1)
    with conn.cursor() as cur:
        cur.execute(
            """
            select exists (
              select 1
              from information_schema.columns
              where table_schema = %(schema_name)s
                and table_name = %(table_name)s
                and column_name = %(column_name)s
            ) as has_col
            """,
            {
                "schema_name": schema_name,
                "table_name": table_name,
                "column_name": column_name,
            },
        )
        row = cur.fetchone() or {}
    return bool(row.get("has_col"))


def _function_exists(conn: Connection, signature: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("select to_regprocedure(%(signature)s) as fn", {"signature": signature})
        row = cur.fetchone() or {}
    return bool(row.get("fn"))


def _relation_supports_transfer_txn(conn: Connection, relation: str) -> bool:
    if "." not in relation:
        return True
    schema_name, table_name = relation.split(".", 1)
    with conn.cursor() as cur:
        cur.execute(
            """
            select pg_get_constraintdef(c.oid) as constraint_def
            from pg_constraint c
            join pg_class t on t.oid = c.conrelid
            join pg_namespace n on n.oid = t.relnamespace
            where c.contype = 'c'
              and c.conname = 'ledger_entries_txn_type_check'
              and n.nspname = %(schema_name)s
              and t.relname = %(table_name)s
            limit 1
            """,
            {"schema_name": schema_name, "table_name": table_name},
        )
        row = cur.fetchone() or {}
    constraint_def = str(row.get("constraint_def") or "").lower()
    if not constraint_def:
        # If the explicit check is not present, assume transfer is acceptable.
        return True
    return "transfer" in constraint_def


def _parse_iso_date_or_raise(raw_value: str) -> date:
    normalized = str(raw_value or "").strip()
    if not normalized:
        raise ApiError(
            status_code=400,
            code="invalid_date",
            message="Date must be YYYY-MM-DD.",
        )
    try:
        parsed = date.fromisoformat(normalized)
    except ValueError as exc:
        raise ApiError(
            status_code=400,
            code="invalid_date",
            message="Date must be YYYY-MM-DD.",
        ) from exc
    if parsed > date.today():
        raise ApiError(
            status_code=400,
            code="future_date_not_allowed",
            message="Future date is not allowed.",
        )
    return parsed


def _legacy_category_relation_for_domain(conn: Connection, domain: str) -> str | None:
    if domain == "product":
        return _first_existing_relation(
            conn,
            ["business.product_categories", "public.product_categories"],
        )
    if domain == "customer":
        return _first_existing_relation(
            conn,
            ["business.customer_categories", "public.customer_categories"],
        )
    if domain == "supplier":
        return _first_existing_relation(
            conn,
            ["business.supplier_categories", "public.supplier_categories"],
        )
    if domain in {"income", "expense"}:
        return _first_existing_relation(
            conn,
            ["personal.categories", "public.categories"],
        )
    return None


def _business_customers_relation(conn: Connection) -> str | None:
    return _first_existing_relation(conn, ["business.customers", "public.customers"])


def _business_suppliers_relation(conn: Connection) -> str | None:
    return _first_existing_relation(conn, ["business.suppliers", "public.suppliers"])


def _business_invoices_relation(conn: Connection) -> str | None:
    return _first_existing_relation(conn, ["business.invoices", "public.invoices"])


def _business_invoice_items_relation(conn: Connection) -> str | None:
    return _first_existing_relation(conn, ["business.invoice_items", "public.invoice_items"])


def _business_invoice_payments_relation(conn: Connection) -> str | None:
    return _first_existing_relation(conn, ["business.invoice_payments", "public.invoice_payments"])


def _business_invoice_documents_relation(conn: Connection) -> str | None:
    return _first_existing_relation(conn, ["business.invoice_documents", "public.invoice_documents"])


def _business_products_relation(conn: Connection) -> str | None:
    return _first_existing_relation(conn, ["business.products", "public.products"])


def _business_accounts_relation(conn: Connection) -> str | None:
    return _first_existing_relation(conn, ["shared.accounts", "business.accounts", "public.accounts"])


def _business_ledger_entries_relation(conn: Connection) -> str | None:
    return _first_existing_relation(conn, ["business.ledger_entries", "public.ledger_entries"])


def _business_ledger_postings_relation(conn: Connection) -> str | None:
    return _first_existing_relation(conn, ["business.ledger_postings", "public.ledger_postings"])


def _existing_relations(conn: Connection, relations: list[str]) -> list[str]:
    existing: list[str] = []
    with conn.cursor() as cur:
        for relation in relations:
            cur.execute("select to_regclass(%(relation)s) as rel", {"relation": relation})
            row = cur.fetchone() or {}
            if row.get("rel"):
                existing.append(relation)
    return existing


def _build_business_account_current_balance_sql(
    conn: Connection,
    *,
    active_only: bool = False,
) -> str:
    accounts_relation = _business_accounts_relation(conn)
    ledger_postings_relation = _business_ledger_postings_relation(conn)
    has_opening_balance = _relation_has_column(conn, accounts_relation or "", "opening_balance")
    has_current_balance = _relation_has_column(conn, accounts_relation or "", "current_balance")
    has_effective_balance_fn = _function_exists(
        conn,
        "public.get_business_account_effective_balance(uuid,uuid,uuid)",
    )

    if has_current_balance and has_opening_balance:
        persisted_balance_expr = "coalesce(current_balance, opening_balance, 0)::numeric"
    elif has_current_balance:
        persisted_balance_expr = "coalesce(current_balance, 0)::numeric"
    elif has_opening_balance:
        persisted_balance_expr = "coalesce(opening_balance, 0)::numeric"
    else:
        persisted_balance_expr = "0::numeric"

    if has_effective_balance_fn:
        computed_balance_expr = (
            "public.get_business_account_effective_balance("
            "%(user_id)s::uuid, %(profile_id)s::uuid, id"
            ")::numeric"
        )
    elif ledger_postings_relation:
        opening_balance_expr = "coalesce(opening_balance, 0)::numeric" if has_opening_balance else "0::numeric"
        postings_balance_expr = (
            "coalesce(("
            f"select sum(case when lp.direction = 'debit' then coalesce(lp.amount, 0) else -coalesce(lp.amount, 0) end)::numeric from {ledger_postings_relation} lp "
            "where lp.user_id = %(user_id)s::uuid "
            "and lp.profile_id = %(profile_id)s::uuid "
            "and lp.leg_type = 'account' "
            "and lp.ref_id = id"
            "), 0::numeric)"
        )
        computed_balance_expr = f"({opening_balance_expr} + {postings_balance_expr})::numeric"
    else:
        computed_balance_expr = persisted_balance_expr

    if active_only:
        return f"{computed_balance_expr} as current_balance"
    return f"case when is_active then {computed_balance_expr} else {persisted_balance_expr} end as current_balance"


def _business_inventory_movements_relation(conn: Connection) -> str | None:
    return _first_existing_relation(conn, ["business.inventory_movements", "public.inventory_movements"])


def _business_product_stock_state_relation(conn: Connection) -> str | None:
    return _first_existing_relation(conn, ["business.product_stock_state", "public.product_stock_state"])


def _load_business_debit_account(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    account_id: str,
    expected_type: str | None = None,
    for_update: bool = False,
) -> dict:
    accounts_relation = _business_accounts_relation(conn)
    if not accounts_relation:
        raise ApiError(
            status_code=500,
            code="business_accounts_table_missing",
            message="Business accounts table is not available.",
        )

    normalized_account_id = str(account_id or "").strip()
    if not normalized_account_id:
        raise ApiError(
            status_code=400,
            code="missing_account",
            message="Payment account is required.",
        )

    normalized_expected_type = str(expected_type or "").strip().lower() or None
    if normalized_expected_type and normalized_expected_type not in {"cash", "bank", "merchant"}:
        raise ApiError(
            status_code=400,
            code="invalid_account_type",
            message="Payment account type must be cash, bank, or merchant.",
        )

    has_allow_overdraft = _relation_has_column(conn, accounts_relation, "allow_overdraft")
    has_overdraft_limit = _relation_has_column(conn, accounts_relation, "overdraft_limit")
    current_balance_sql = _build_business_account_current_balance_sql(conn, active_only=True)
    type_filter_sql = (
        "and type = %(expected_type)s::text"
        if normalized_expected_type
        else "and type in ('cash', 'bank', 'merchant')"
    )
    lock_sql = "for update" if for_update else ""

    with conn.cursor() as cur:
        cur.execute(
            f"""
            select
              id::text as id,
              name,
              type,
              {current_balance_sql},
              {"coalesce(allow_overdraft, false)" if has_allow_overdraft else "false"} as allow_overdraft,
              {"coalesce(overdraft_limit, 0)::numeric" if has_overdraft_limit else "0::numeric"} as overdraft_limit
            from {accounts_relation}
            where id = %(account_id)s::uuid
              and user_id = %(user_id)s::uuid
              and profile_id = %(profile_id)s::uuid
              and is_active = true
              {type_filter_sql}
            {lock_sql}
            limit 1
            """,
            {
                "account_id": normalized_account_id,
                "user_id": user_id,
                "profile_id": profile_id,
                "expected_type": normalized_expected_type,
            },
        )
        row = cur.fetchone() or {}

    if not row:
        raise ApiError(
            status_code=400,
            code="invalid_account",
            message="Invalid payment account for this profile.",
        )
    return row


def _ensure_business_account_debit_capacity(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    account_id: str,
    amount: float,
    expected_type: str | None = None,
    for_update: bool = False,
) -> None:
    normalized_amount = round(max(0.0, float(amount or 0)), 2)
    if normalized_amount <= 0:
        return

    account_row = _load_business_debit_account(
        conn,
        user_id=user_id,
        profile_id=profile_id,
        account_id=account_id,
        expected_type=expected_type,
        for_update=for_update,
    )
    current_balance = float(account_row.get("current_balance") or 0)
    if (current_balance - normalized_amount) < 0.0:
        raise ApiError(
            status_code=400,
            code="insufficient_balance",
            message="Insufficient balance. Please transfer from another account.",
        )


def _assert_business_ledger_entry_is_balanced(
    conn: Connection,
    *,
    ledger_postings_relation: str,
    entry_id: str,
) -> None:
    normalized_entry_id = str(entry_id or "").strip()
    if not normalized_entry_id:
        raise ApiError(
            status_code=500,
            code="ledger_entry_missing",
            message="Ledger entry id is missing for balance verification.",
        )

    with conn.cursor() as cur:
        cur.execute(
            f"""
            select
              count(*) filter (where direction = 'debit') as debit_count,
              count(*) filter (where direction = 'credit') as credit_count,
              coalesce(
                sum(
                  case
                    when direction = 'debit' then coalesce(amount, 0)
                    when direction = 'credit' then -coalesce(amount, 0)
                    else 0
                  end
                ),
                0
              )::numeric as net_delta
            from {ledger_postings_relation}
            where entry_id = %(entry_id)s::uuid
            """,
            {"entry_id": normalized_entry_id},
        )
        check_row = cur.fetchone() or {}

    debit_count = int(check_row.get("debit_count") or 0)
    credit_count = int(check_row.get("credit_count") or 0)
    net_delta = Decimal(str(check_row.get("net_delta") or 0))
    is_balanced = abs(net_delta) <= Decimal("0.0001")
    has_double_entry = debit_count > 0 and credit_count > 0
    if is_balanced and has_double_entry:
        return

    raise ApiError(
        status_code=500,
        code="unbalanced_ledger_entry",
        message="Ledger entry must follow double-entry bookkeeping (balanced debit/credit).",
    )


def _collect_customer_receivable_direct(
    conn: Connection,
    *,
    auth_user_id: str,
    profile_id: str,
    customer_id: str,
    amount: float,
    account_id: str,
    entry_date: date | None,
    note: str | None,
) -> str:
    customers_relation = _business_customers_relation(conn)
    ledger_entries_relation = _business_ledger_entries_relation(conn)
    ledger_postings_relation = _business_ledger_postings_relation(conn)
    if not customers_relation or not ledger_entries_relation or not ledger_postings_relation:
        raise ApiError(
            status_code=500,
            code="business_ledger_tables_missing",
            message="Business ledger/customer tables are not available.",
        )

    if amount <= 0:
        raise ApiError(
            status_code=400,
            code="invalid_amount",
            message="Collection amount must be greater than zero.",
        )

    if entry_date is not None and entry_date > date.today():
        raise ApiError(
            status_code=400,
            code="invalid_date",
            message="Future dates are not allowed.",
        )

    has_assert_active_profile = _function_exists(
        conn,
        "public.assert_active_business_profile(uuid,uuid)",
    )
    has_assert_profile_owner = _function_exists(
        conn,
        "public.assert_profile_ownership(uuid,uuid)",
    )
    has_counterparty_col = _relation_has_column(conn, ledger_entries_relation, "counterparty_id")
    has_metadata_col = _relation_has_column(conn, ledger_entries_relation, "metadata")

    with conn.transaction():
        with conn.cursor() as cur:
            if has_assert_active_profile:
                cur.execute(
                    "select public.assert_active_business_profile(%(user_id)s::uuid, %(profile_id)s::uuid)",
                    {"user_id": auth_user_id, "profile_id": profile_id},
                )
            elif has_assert_profile_owner:
                cur.execute(
                    "select public.assert_profile_ownership(%(user_id)s::uuid, %(profile_id)s::uuid)",
                    {"user_id": auth_user_id, "profile_id": profile_id},
                )

            cur.execute(
                """
                select 1
                from public.accounts a
                where a.id = %(account_id)s::uuid
                  and a.user_id = %(user_id)s::uuid
                  and a.profile_id = %(profile_id)s::uuid
                  and a.is_active = true
                  and a.type in ('cash', 'bank', 'merchant')
                limit 1
                """,
                {
                    "account_id": account_id,
                    "user_id": auth_user_id,
                    "profile_id": profile_id,
                },
            )
            if not cur.fetchone():
                raise ApiError(
                    status_code=400,
                    code="invalid_account",
                    message="Invalid account for this profile.",
                )

            cur.execute(
                f"""
                select c.name
                from {customers_relation} c
                where c.id = %(customer_id)s::uuid
                  and c.user_id = %(user_id)s::uuid
                  and c.profile_id = %(profile_id)s::uuid
                limit 1
                """,
                {
                    "customer_id": customer_id,
                    "user_id": auth_user_id,
                    "profile_id": profile_id,
                },
            )
            customer_row = cur.fetchone() or {}
            customer_name = str(customer_row.get("name") or "").strip()
            if not customer_name:
                raise ApiError(
                    status_code=404,
                    code="customer_not_found",
                    message="Customer not found for this profile.",
                )

            cur.execute(
                f"""
                select coalesce(
                  sum(
                    case
                      when lp.direction = 'debit' then lp.amount
                      when lp.direction = 'credit' then -lp.amount
                      else 0
                    end
                  ),
                  0
                ) as due_amount
                from {ledger_postings_relation} lp
                where lp.user_id = %(user_id)s::uuid
                  and lp.profile_id = %(profile_id)s::uuid
                  and lp.leg_type = 'receivable'
                  and lp.ref_id = %(customer_id)s::uuid
                """,
                {
                    "user_id": auth_user_id,
                    "profile_id": profile_id,
                    "customer_id": customer_id,
                },
            )
            due_row = cur.fetchone() or {}
            due_amount = float(due_row.get("due_amount") or 0)
            due_amount = max(0.0, round(due_amount, 2))

            if due_amount <= 0:
                raise ApiError(
                    status_code=400,
                    code="no_customer_due",
                    message="No pending receivable due for this customer.",
                )

            amount_rounded = round(float(amount), 2)
            if amount_rounded > due_amount:
                raise ApiError(
                    status_code=400,
                    code="customer_due_exceeded",
                    message="Collection exceeds pending customer due amount.",
                )

            description = f"Customer due collection | Customer: {customer_name}"
            if note and note.strip():
                description += f" | Note: {note.strip()}"

            entry_columns = [
                "user_id",
                "profile_id",
                "txn_type",
                "amount",
                "date",
                "description",
                "account_id",
            ]
            entry_values = [
                "%(user_id)s::uuid",
                "%(profile_id)s::uuid",
                "'receivable_collection'",
                "%(amount)s::numeric",
                "%(entry_date)s::date",
                "%(description)s",
                "%(account_id)s::uuid",
            ]
            entry_bind: dict[str, object] = {
                "user_id": auth_user_id,
                "profile_id": profile_id,
                "amount": amount_rounded,
                "entry_date": (entry_date or date.today()).isoformat(),
                "description": description,
                "account_id": account_id,
            }
            if has_counterparty_col:
                entry_columns.append("counterparty_id")
                entry_values.append("%(customer_id)s::uuid")
                entry_bind["customer_id"] = customer_id
            if has_metadata_col:
                entry_columns.append("metadata")
                entry_values.append("%(metadata)s::jsonb")
                entry_bind["metadata"] = Jsonb(
                    {
                        "operation": "customer_due_collection",
                        "source": "business_transactions",
                        "customer_id": customer_id,
                        "customer_name": customer_name,
                        "account_id": account_id,
                    }
                )

            cur.execute(
                f"""
                insert into {ledger_entries_relation} ({", ".join(entry_columns)})
                values ({", ".join(entry_values)})
                returning id::text as entry_id
                """,
                entry_bind,
            )
            entry_row = cur.fetchone() or {}
            entry_id = str(entry_row.get("entry_id") or "").strip()
            if not entry_id:
                raise ApiError(
                    status_code=500,
                    code="receivable_collection_insert_failed",
                    message="Failed to create receivable collection ledger entry.",
                )

            cur.execute(
                f"""
                insert into {ledger_postings_relation} (
                  entry_id, user_id, profile_id, leg_type, ref_id, direction, amount
                )
                values
                  (%(entry_id)s::uuid, %(user_id)s::uuid, %(profile_id)s::uuid, 'account', %(account_id)s::uuid, 'debit', %(amount)s::numeric),
                  (%(entry_id)s::uuid, %(user_id)s::uuid, %(profile_id)s::uuid, 'receivable', %(customer_id)s::uuid, 'credit', %(amount)s::numeric)
                """,
                {
                    "entry_id": entry_id,
                    "user_id": auth_user_id,
                    "profile_id": profile_id,
                    "account_id": account_id,
                    "customer_id": customer_id,
                    "amount": amount_rounded,
                },
            )

            _assert_business_ledger_entry_is_balanced(
                conn,
                ledger_postings_relation=ledger_postings_relation,
                entry_id=entry_id,
            )

    return entry_id


def _list_business_suppliers_direct(
    conn: Connection,
    *,
    auth_user_id: str,
    profile_id: str,
    include_inactive: bool,
) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "select public.assert_active_business_profile(%(user_id)s::uuid, %(profile_id)s::uuid)",
            {"user_id": auth_user_id, "profile_id": profile_id},
        )

    relation = _business_suppliers_relation(conn)
    if not relation:
        raise ApiError(
            status_code=500,
            code="suppliers_table_missing",
            message="Supplier table is not available.",
        )

    has_phone = _relation_has_column(conn, relation, "phone")
    has_address = _relation_has_column(conn, relation, "address")
    has_opening_balance = _relation_has_column(conn, relation, "opening_balance")
    has_opening_balance_type = _relation_has_column(conn, relation, "opening_balance_type")
    has_category_id = _relation_has_column(conn, relation, "category_id")
    has_reminder_date = _relation_has_column(conn, relation, "reminder_date")
    has_is_active = _relation_has_column(conn, relation, "is_active")
    has_created_at = _relation_has_column(conn, relation, "created_at")
    has_updated_at = _relation_has_column(conn, relation, "updated_at")
    status_sql = "" if include_inactive or not has_is_active else "and s.is_active = true"

    query = f"""
        select
          s.id::text as id,
          s.user_id::text as user_id,
          s.profile_id::text as profile_id,
          s.name,
          {"s.phone" if has_phone else "null::text"} as phone,
          {"s.address" if has_address else "null::text"} as address,
          {"s.opening_balance" if has_opening_balance else "0::numeric"} as opening_balance,
          {"s.opening_balance_type" if has_opening_balance_type else "'credit'::text"} as opening_balance_type,
          {"s.category_id::text as category_id" if has_category_id else "null::text as category_id"},
          {"s.reminder_date::text as reminder_date" if has_reminder_date else "null::text as reminder_date"},
          {"s.is_active" if has_is_active else "true"} as is_active,
          {"s.created_at::text" if has_created_at else "now()::text"} as created_at,
          {"s.updated_at::text" if has_updated_at else "now()::text"} as updated_at
        from {relation} s
        where s.user_id = %(user_id)s::uuid
          and s.profile_id = %(profile_id)s::uuid
          {status_sql}
        order by {"s.created_at asc" if has_created_at else "s.id asc"}
    """

    with conn.cursor() as cur:
        cur.execute(
            query,
            {"user_id": auth_user_id, "profile_id": profile_id},
        )
        rows = cur.fetchall() or []
    return rows


def _load_business_supplier_item(
    conn: Connection,
    *,
    auth_user_id: str,
    profile_id: str,
    supplier_id: str,
) -> dict | None:
    relation = _business_suppliers_relation(conn)
    if not relation:
        return None

    has_phone = _relation_has_column(conn, relation, "phone")
    has_address = _relation_has_column(conn, relation, "address")
    has_opening_balance = _relation_has_column(conn, relation, "opening_balance")
    has_opening_balance_type = _relation_has_column(conn, relation, "opening_balance_type")
    has_category_id = _relation_has_column(conn, relation, "category_id")
    has_reminder_date = _relation_has_column(conn, relation, "reminder_date")
    has_is_active = _relation_has_column(conn, relation, "is_active")
    has_created_at = _relation_has_column(conn, relation, "created_at")
    has_updated_at = _relation_has_column(conn, relation, "updated_at")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            select
              s.id::text as id,
              s.user_id::text as user_id,
              s.profile_id::text as profile_id,
              s.name,
              {"s.phone" if has_phone else "null::text"} as phone,
              {"s.address" if has_address else "null::text"} as address,
              {"s.opening_balance" if has_opening_balance else "0::numeric"} as opening_balance,
              {"s.opening_balance_type" if has_opening_balance_type else "'credit'::text"} as opening_balance_type,
              {"s.category_id::text as category_id" if has_category_id else "null::text as category_id"},
              {"s.reminder_date::text as reminder_date" if has_reminder_date else "null::text as reminder_date"},
              {"s.is_active" if has_is_active else "true"} as is_active,
              {"s.created_at::text" if has_created_at else "now()::text"} as created_at,
              {"s.updated_at::text" if has_updated_at else "now()::text"} as updated_at
            from {relation} s
            where s.user_id = %(user_id)s::uuid
              and s.profile_id = %(profile_id)s::uuid
              and s.id = %(supplier_id)s::uuid
            limit 1
            """,
            {
                "user_id": auth_user_id,
                "profile_id": profile_id,
                "supplier_id": supplier_id,
            },
        )
        return cur.fetchone() or None


def _load_business_supplier_item_by_name(
    conn: Connection,
    *,
    auth_user_id: str,
    profile_id: str,
    supplier_name: str,
) -> dict | None:
    relation = _business_suppliers_relation(conn)
    if not relation:
        return None

    normalized_name = str(supplier_name or "").strip()
    if not normalized_name:
        return None

    has_phone = _relation_has_column(conn, relation, "phone")
    has_address = _relation_has_column(conn, relation, "address")
    has_opening_balance = _relation_has_column(conn, relation, "opening_balance")
    has_opening_balance_type = _relation_has_column(conn, relation, "opening_balance_type")
    has_category_id = _relation_has_column(conn, relation, "category_id")
    has_reminder_date = _relation_has_column(conn, relation, "reminder_date")
    has_is_active = _relation_has_column(conn, relation, "is_active")
    has_created_at = _relation_has_column(conn, relation, "created_at")
    has_updated_at = _relation_has_column(conn, relation, "updated_at")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            select
              s.id::text as id,
              s.user_id::text as user_id,
              s.profile_id::text as profile_id,
              s.name,
              {"s.phone" if has_phone else "null::text"} as phone,
              {"s.address" if has_address else "null::text"} as address,
              {"s.opening_balance" if has_opening_balance else "0::numeric"} as opening_balance,
              {"s.opening_balance_type" if has_opening_balance_type else "'credit'::text"} as opening_balance_type,
              {"s.category_id::text as category_id" if has_category_id else "null::text as category_id"},
              {"s.reminder_date::text as reminder_date" if has_reminder_date else "null::text as reminder_date"},
              {"s.is_active" if has_is_active else "true"} as is_active,
              {"s.created_at::text" if has_created_at else "now()::text"} as created_at,
              {"s.updated_at::text" if has_updated_at else "now()::text"} as updated_at
            from {relation} s
            where s.user_id = %(user_id)s::uuid
              and s.profile_id = %(profile_id)s::uuid
              and lower(trim(s.name)) = lower(trim(%(supplier_name)s))
            order by {"s.is_active desc," if has_is_active else ""} {"s.created_at desc" if has_created_at else "s.id desc"}
            limit 1
            """,
            {
                "user_id": auth_user_id,
                "profile_id": profile_id,
                "supplier_name": normalized_name,
            },
        )
        return cur.fetchone() or None


def _create_business_supplier_direct(
    conn: Connection,
    *,
    auth_user_id: str,
    profile_id: str,
    name: str,
    phone: str | None,
    address: str | None,
    category_id: str | None,
    opening_balance: float,
    opening_balance_type: str,
    reminder_date: str | None,
) -> dict:
    relation = _business_suppliers_relation(conn)
    if not relation:
        raise ApiError(
            status_code=500,
            code="suppliers_table_missing",
            message="Supplier table is not available.",
        )

    normalized_name = str(name or "").strip()
    if not normalized_name:
        raise ApiError(
            status_code=400,
            code="name_required",
            message="name is required.",
        )

    has_profile_id = _relation_has_column(conn, relation, "profile_id")
    has_phone = _relation_has_column(conn, relation, "phone")
    has_address = _relation_has_column(conn, relation, "address")
    has_category_id = _relation_has_column(conn, relation, "category_id")
    has_opening_balance = _relation_has_column(conn, relation, "opening_balance")
    has_opening_balance_type = _relation_has_column(conn, relation, "opening_balance_type")
    has_reminder_date = _relation_has_column(conn, relation, "reminder_date")
    has_is_active = _relation_has_column(conn, relation, "is_active")
    has_updated_at = _relation_has_column(conn, relation, "updated_at")

    try:
        with conn.cursor() as cur:
            where_parts = [
                "user_id = %(user_id)s::uuid",
                "lower(trim(name)) = lower(trim(%(name)s))",
            ]
            if has_profile_id:
                where_parts.append("profile_id = %(profile_id)s::uuid")
            cur.execute(
                f"""
                select id::text as id
                from {relation}
                where {" and ".join(where_parts)}
                order by {"is_active desc," if has_is_active else ""} created_at asc
                limit 1
                """,
                {
                    "user_id": auth_user_id,
                    "profile_id": profile_id,
                    "name": normalized_name,
                },
            )
            existing_row = cur.fetchone() or {}
            existing_supplier_id = str(existing_row.get("id") or "").strip()

            if existing_supplier_id:
                set_parts = ["name = %(name)s::text"]
                bind: dict[str, object] = {
                    "supplier_id": existing_supplier_id,
                    "user_id": auth_user_id,
                    "profile_id": profile_id,
                    "name": normalized_name,
                    "phone": phone,
                    "address": address,
                    "category_id": category_id,
                    "opening_balance": opening_balance,
                    "opening_balance_type": opening_balance_type,
                    "reminder_date": reminder_date,
                }
                if has_is_active:
                    set_parts.append("is_active = true")
                if has_phone:
                    set_parts.append("phone = %(phone)s")
                if has_address:
                    set_parts.append("address = %(address)s")
                if has_category_id:
                    set_parts.append("category_id = %(category_id)s::uuid")
                if has_opening_balance:
                    set_parts.append("opening_balance = %(opening_balance)s::numeric")
                if has_opening_balance_type:
                    set_parts.append("opening_balance_type = %(opening_balance_type)s::text")
                if has_reminder_date:
                    set_parts.append("reminder_date = %(reminder_date)s::date")
                if has_updated_at:
                    set_parts.append("updated_at = now()")

                where_update = [
                    "id = %(supplier_id)s::uuid",
                    "user_id = %(user_id)s::uuid",
                ]
                if has_profile_id:
                    where_update.append("profile_id = %(profile_id)s::uuid")
                cur.execute(
                    f"""
                    update {relation}
                       set {", ".join(set_parts)}
                     where {" and ".join(where_update)}
                    returning id::text as id
                    """,
                    bind,
                )
                updated = cur.fetchone() or {}
                updated_id = str(updated.get("id") or existing_supplier_id).strip()
                loaded = _load_business_supplier_item(
                    conn,
                    auth_user_id=auth_user_id,
                    profile_id=profile_id,
                    supplier_id=updated_id,
                )
                if loaded:
                    return loaded

            insert_fields = ["user_id", "name"]
            insert_values = ["%(user_id)s::uuid", "%(name)s::text"]
            insert_bind: dict[str, object] = {
                "user_id": auth_user_id,
                "profile_id": profile_id,
                "name": normalized_name,
                "phone": phone,
                "address": address,
                "category_id": category_id,
                "opening_balance": opening_balance,
                "opening_balance_type": opening_balance_type,
                "reminder_date": reminder_date,
            }
            if has_profile_id:
                insert_fields.append("profile_id")
                insert_values.append("%(profile_id)s::uuid")
            if has_phone:
                insert_fields.append("phone")
                insert_values.append("%(phone)s::text")
            if has_address:
                insert_fields.append("address")
                insert_values.append("%(address)s::text")
            if has_category_id:
                insert_fields.append("category_id")
                insert_values.append("%(category_id)s::uuid")
            if has_opening_balance:
                insert_fields.append("opening_balance")
                insert_values.append("%(opening_balance)s::numeric")
            if has_opening_balance_type:
                insert_fields.append("opening_balance_type")
                insert_values.append("%(opening_balance_type)s::text")
            if has_reminder_date:
                insert_fields.append("reminder_date")
                insert_values.append("%(reminder_date)s::date")
            if has_is_active:
                insert_fields.append("is_active")
                insert_values.append("true")

            cur.execute(
                f"""
                insert into {relation} ({", ".join(insert_fields)})
                values ({", ".join(insert_values)})
                returning id::text as id
                """,
                insert_bind,
            )
            created = cur.fetchone() or {}
            created_id = str(created.get("id") or "").strip()
            if created_id:
                loaded = _load_business_supplier_item(
                    conn,
                    auth_user_id=auth_user_id,
                    profile_id=profile_id,
                    supplier_id=created_id,
                )
                if loaded:
                    return loaded
    except UniqueViolation as exc:
        raise ApiError(
            status_code=409,
            code="supplier_name_exists",
            message="Supplier with this name already exists in this profile.",
        ) from exc
    except PsycopgError as exc:
        raise ApiError(
            status_code=400,
            code="business_supplier_create_failed",
            message=str(exc).strip() or "Failed to create supplier.",
        ) from exc

    loaded_by_name = _load_business_supplier_item_by_name(
        conn,
        auth_user_id=auth_user_id,
        profile_id=profile_id,
        supplier_name=normalized_name,
    )
    if loaded_by_name:
        return loaded_by_name

    raise ApiError(
        status_code=500,
        code="supplier_create_unresolved",
        message="Supplier was created but could not be loaded. Please refresh and try again.",
    )


def _force_legacy_category_domain(domain: str) -> bool:
    # Product CRUD must stay on product category tables because product RPCs/foreign keys
    # reference those tables directly.
    return domain == "product"


@router.post("/rpc", response_model=BusinessRpcResponse)
def post_business_rpc(
    payload: BusinessRpcRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> BusinessRpcResponse:
    apply_db_auth_context(conn, auth.user_id)
    params = payload.params or {}

    if payload.name == "list_business_units":
        profile_id = str(params.get("p_profile_id") or "").strip()
        if not profile_id:
            raise ApiError(
                status_code=400,
                code="invalid_rpc_param",
                message="Missing required RPC parameter: p_profile_id",
            )
        data = _list_business_units_direct(
            conn,
            auth_user_id=auth.user_id,
            profile_id=profile_id,
        )
        return BusinessRpcResponse(data=_to_json_safe(data))

    if payload.name == "create_business_unit":
        profile_id = str(params.get("p_profile_id") or "").strip()
        name = str(params.get("p_name") or "").strip()
        if not profile_id:
            raise ApiError(
                status_code=400,
                code="invalid_rpc_param",
                message="Missing required RPC parameter: p_profile_id",
            )
        if not name:
            raise ApiError(
                status_code=400,
                code="invalid_rpc_param",
                message="Missing required RPC parameter: p_name",
            )
        data = _create_business_unit_direct(
            conn,
            auth_user_id=auth.user_id,
            profile_id=profile_id,
            name=name,
        )
        return BusinessRpcResponse(data=_to_json_safe(data))

    if payload.name == "replace_business_unit_products":
        profile_id = str(params.get("p_profile_id") or "").strip()
        from_unit_id = str(params.get("p_from_unit_id") or "").strip()
        to_unit_id = str(params.get("p_to_unit_id") or "").strip()
        if not profile_id:
            raise ApiError(
                status_code=400,
                code="invalid_rpc_param",
                message="Missing required RPC parameter: p_profile_id",
            )
        if not from_unit_id:
            raise ApiError(
                status_code=400,
                code="invalid_rpc_param",
                message="Missing required RPC parameter: p_from_unit_id",
            )
        if not to_unit_id:
            raise ApiError(
                status_code=400,
                code="invalid_rpc_param",
                message="Missing required RPC parameter: p_to_unit_id",
            )
        data = _replace_business_unit_products_direct(
            conn,
            auth_user_id=auth.user_id,
            profile_id=profile_id,
            from_unit_id=from_unit_id,
            to_unit_id=to_unit_id,
        )
        _enqueue_business_ai_refresh(
            conn,
            user_id=auth.user_id,
            profile_id=profile_id,
        )
        return BusinessRpcResponse(data=_to_json_safe(data))

    if payload.name == "list_business_products":
        profile_id = str(params.get("p_profile_id") or "").strip()
        if not profile_id:
            raise ApiError(
                status_code=400,
                code="invalid_rpc_param",
                message="Missing required RPC parameter: p_profile_id",
            )
        data = _list_business_products_direct(
            conn,
            auth_user_id=auth.user_id,
            profile_id=profile_id,
        )
        return BusinessRpcResponse(data=_to_json_safe(data))

    if payload.name == "create_business_account":
        _ensure_active_business_profile_for_account_create(
            conn,
            auth_user_id=auth.user_id,
            params=params,
        )

    if payload.name == "list_business_suppliers":
        profile_id = str(params.get("p_profile_id") or "").strip()
        if not profile_id:
            raise ApiError(
                status_code=400,
                code="invalid_rpc_param",
                message="Missing required RPC parameter: p_profile_id",
            )
        raw_include_inactive = params.get("p_include_inactive", False)
        include_inactive = bool(raw_include_inactive)
        if isinstance(raw_include_inactive, str):
            include_inactive = raw_include_inactive.strip().lower() in {"1", "true", "yes", "on"}
        data = _list_business_suppliers_direct(
            conn,
            auth_user_id=auth.user_id,
            profile_id=profile_id,
            include_inactive=include_inactive,
        )
        return BusinessRpcResponse(data=_to_json_safe(data))

    if payload.name in {
        "create_stock_in_entry",
        "create_stock_in_with_product",
        "create_stock_in_with_product_source",
    }:
        profile_id = str(params.get("p_profile_id") or "").strip()
        payment_mode = str(params.get("p_payment_mode") or "").strip().lower()
        account_id = str(params.get("p_account_id") or "").strip()
        qty_value = float(params.get("p_qty") or 0)
        unit_cost_value = float(params.get("p_unit_cost") or 0)
        total_amount = round(max(0.0, qty_value * unit_cost_value), 2)
        raw_paid_amount = params.get("p_paid_amount")
        if payment_mode in {"cash", "bank", "merchant"}:
            account_applied = total_amount
            if raw_paid_amount is not None:
                account_applied = round(
                    min(total_amount, max(0.0, float(raw_paid_amount or 0))),
                    2,
                )
            _ensure_business_account_debit_capacity(
                conn,
                user_id=auth.user_id,
                profile_id=profile_id,
                account_id=account_id,
                amount=account_applied,
                expected_type=payment_mode,
            )

    if payload.name == "repay_business_payable":
        profile_id = str(params.get("p_profile_id") or "").strip()
        account_id = str(params.get("p_account_id") or "").strip()
        amount_value = round(max(0.0, float(params.get("p_amount") or 0)), 2)
        _ensure_business_account_debit_capacity(
            conn,
            user_id=auth.user_id,
            profile_id=profile_id,
            account_id=account_id,
            amount=amount_value,
        )

    if payload.name == "collect_customer_receivable_entry":
        profile_id = str(params.get("p_profile_id") or "").strip()
        customer_id = str(params.get("p_customer_id") or "").strip()
        account_id = str(params.get("p_account_id") or "").strip()
        raw_amount = params.get("p_amount")
        raw_date = params.get("p_date")
        note = params.get("p_note")

        if not profile_id:
            raise ApiError(
                status_code=400,
                code="invalid_rpc_param",
                message="Missing required RPC parameter: p_profile_id",
            )
        if not customer_id:
            raise ApiError(
                status_code=400,
                code="invalid_rpc_param",
                message="Missing required RPC parameter: p_customer_id",
            )
        if not account_id:
            raise ApiError(
                status_code=400,
                code="invalid_rpc_param",
                message="Missing required RPC parameter: p_account_id",
            )
        try:
            amount_value = float(raw_amount)
        except (TypeError, ValueError):
            raise ApiError(
                status_code=400,
                code="invalid_rpc_param",
                message="Missing or invalid RPC parameter: p_amount",
            )

        entry_date_value: date | None = None
        if raw_date is not None and str(raw_date).strip():
            entry_date_value = _parse_iso_date_or_raise(str(raw_date))

        try:
            data = _execute_named_rpc(conn, payload.name, params)
            _enqueue_business_ai_refresh(
                conn,
                user_id=auth.user_id,
                profile_id=profile_id,
            )
            return BusinessRpcResponse(data=_to_json_safe(data))
        except ApiError as exc:
            if exc.code != "business_rpc_missing":
                raise

        fallback_entry_id = _collect_customer_receivable_direct(
            conn,
            auth_user_id=auth.user_id,
            profile_id=profile_id,
            customer_id=customer_id,
            amount=amount_value,
            account_id=account_id,
            entry_date=entry_date_value,
            note=str(note).strip() if note is not None else None,
        )
        _enqueue_business_ai_refresh(
            conn,
            user_id=auth.user_id,
            profile_id=profile_id,
        )
        return BusinessRpcResponse(data=_to_json_safe(fallback_entry_id))

    data = _execute_named_rpc(conn, payload.name, params)
    if payload.name in _BUSINESS_AI_WRITE_RPC_NAMES:
        _enqueue_business_ai_refresh(
            conn,
            user_id=auth.user_id,
            profile_id=str(params.get("p_profile_id") or "").strip() or None,
        )
    return BusinessRpcResponse(data=_to_json_safe(data))


@router.get("/accounts", response_model=BusinessAccountsResponse)
def get_business_accounts(
    profile_id: str = Query(...),
    include_inactive: bool = Query(default=False),
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> BusinessAccountsResponse:
    apply_db_auth_context(conn, auth.user_id)
    accounts_relation = _business_accounts_relation(conn)
    if not accounts_relation:
        raise ApiError(
            status_code=500,
            code="business_accounts_table_missing",
            message="Business accounts table is not available.",
        )
    status_sql = "" if include_inactive else "and is_active = true"
    has_institution_name = _relation_has_column(conn, accounts_relation, "institution_name")
    has_account_number = _relation_has_column(conn, accounts_relation, "account_number")
    has_qr_image_url = _relation_has_column(conn, accounts_relation, "qr_image_url")
    has_opening_balance = _relation_has_column(conn, accounts_relation, "opening_balance")
    has_overdraft_limit = _relation_has_column(conn, accounts_relation, "overdraft_limit")
    current_balance_sql = _build_business_account_current_balance_sql(conn)
    query = f"""
        select
          id, user_id, profile_id, name, type,
          {"institution_name" if has_institution_name else "null::text as institution_name"},
          {"account_number" if has_account_number else "null::text as account_number"},
          {"qr_image_url" if has_qr_image_url else "null::text as qr_image_url"},
          {"opening_balance" if has_opening_balance else "null::numeric as opening_balance"},
          {current_balance_sql},
          {"overdraft_limit" if has_overdraft_limit else "null::numeric as overdraft_limit"},
          is_active, created_at
        from {accounts_relation}
        where user_id = %(user_id)s
          and profile_id = %(profile_id)s
          and type in ('cash', 'bank', 'merchant')
          {status_sql}
        order by created_at asc
    """
    with conn.cursor() as cur:
        cur.execute(
            query,
            {"user_id": auth.user_id, "profile_id": profile_id},
        )
        rows = cur.fetchall() or []
    return BusinessAccountsResponse(items=rows)


@router.get("/accounts/names", response_model=BusinessAccountNamesResponse)
def get_business_account_names(
    profile_id: str = Query(...),
    ids: str = Query(...),
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> BusinessAccountNamesResponse:
    apply_db_auth_context(conn, auth.user_id)
    accounts_relation = _business_accounts_relation(conn)
    if not accounts_relation:
        raise ApiError(
            status_code=500,
            code="business_accounts_table_missing",
            message="Business accounts table is not available.",
        )
    account_ids = [part.strip() for part in ids.split(",") if part.strip()]
    if not account_ids:
        return BusinessAccountNamesResponse(items=[])

    query = f"""
        select id, name, type
        from {accounts_relation}
        where user_id = %(user_id)s
          and profile_id = %(profile_id)s
          and id = any(%(account_ids)s::uuid[])
    """
    with conn.cursor() as cur:
        cur.execute(
            query,
            {
                "user_id": auth.user_id,
                "profile_id": profile_id,
                "account_ids": account_ids,
            },
        )
        rows = cur.fetchall() or []
    return BusinessAccountNamesResponse(items=rows)


@router.get("/transactions/feed", response_model=BusinessTransactionsFeedResponse)
def get_business_transactions_feed(
    profile_id: str = Query(...),
    limit: int = Query(default=30, ge=1, le=100),
    cursor: str | None = Query(default=None),
    period: str = Query(default="all"),
    from_date: date | None = Query(default=None),
    to_date: date | None = Query(default=None),
    section: str | None = Query(default=None),
    search: str | None = Query(default=None),
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> BusinessTransactionsFeedResponse:
    apply_db_auth_context(conn, auth.user_id)
    normalized_section = str(section or "").strip().lower() or None
    if normalized_section and normalized_section not in {"posting", "customer", "supplier"}:
        raise ApiError(
            status_code=400,
            code="invalid_section",
            message="section must be one of posting, customer, or supplier.",
        )
    payload = fetch_business_transactions_feed(
        conn,
        user_id=auth.user_id,
        profile_id=profile_id,
        limit=limit,
        cursor=cursor,
        period=period,
        from_date=from_date,
        to_date=to_date,
        section=normalized_section,  # type: ignore[arg-type]
        search=search,
    )
    return BusinessTransactionsFeedResponse(**payload)


@router.get("/pos/bootstrap", response_model=BusinessPosBootstrapResponse)
def get_business_pos_bootstrap(
    profile_id: str = Query(...),
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> BusinessPosBootstrapResponse:
    apply_db_auth_context(conn, auth.user_id)
    payload = fetch_business_pos_bootstrap(
        conn,
        user_id=auth.user_id,
        profile_id=profile_id,
    )
    return BusinessPosBootstrapResponse(**payload)


@router.get("/customers/search", response_model=BusinessCustomerSearchResponse)
def search_business_customers_endpoint(
    profile_id: str = Query(...),
    q: str = Query(...),
    limit: int = Query(default=20, ge=1, le=50),
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> BusinessCustomerSearchResponse:
    apply_db_auth_context(conn, auth.user_id)
    items = search_business_customers(
        conn,
        user_id=auth.user_id,
        profile_id=profile_id,
        query=q,
        limit=limit,
    )
    safe_items = _to_json_safe(items)
    return BusinessCustomerSearchResponse(
        items=safe_items if isinstance(safe_items, list) else []
    )


@router.get("/reports/product-sales", response_model=BusinessProductSalesSummaryResponse)
def get_business_product_sales_summary(
    profile_id: str = Query(...),
    product_id: str = Query(...),
    period: str = Query(default="all"),
    from_date: date | None = Query(default=None),
    to_date: date | None = Query(default=None),
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> BusinessProductSalesSummaryResponse:
    apply_db_auth_context(conn, auth.user_id)
    payload = fetch_business_product_sales_summary(
        conn,
        user_id=auth.user_id,
        profile_id=profile_id,
        product_id=product_id,
        period=period,
        from_date=from_date,
        to_date=to_date,
    )
    return BusinessProductSalesSummaryResponse(**payload)


@router.post("/accounts/transfer", response_model=BusinessAccountTransferResponse)
def post_business_account_transfer(
    payload: BusinessAccountTransferRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> BusinessAccountTransferResponse:
    apply_db_auth_context(conn, auth.user_id)
    accounts_relation = _business_accounts_relation(conn)
    if not accounts_relation:
        raise ApiError(
            status_code=500,
            code="business_accounts_table_missing",
            message="Business accounts table is not available.",
        )
    profile_id = str(payload.profile_id or "").strip()
    from_account_id = str(payload.from_account_id or "").strip()
    to_account_id = str(payload.to_account_id or "").strip()
    amount = float(payload.amount)

    if not profile_id:
        raise ApiError(
            status_code=400,
            code="invalid_profile_id",
            message="profile_id is required.",
        )
    if not from_account_id or not to_account_id:
        raise ApiError(
            status_code=400,
            code="invalid_account_ids",
            message="Source and destination accounts are required.",
        )
    if from_account_id == to_account_id:
        raise ApiError(
            status_code=400,
            code="same_account_transfer",
            message="Source and destination accounts must be different.",
        )
    if amount <= 0:
        raise ApiError(
            status_code=400,
            code="invalid_amount",
            message="Amount must be greater than zero.",
        )

    transfer_date = _parse_iso_date_or_raise(payload.date)

    with conn.cursor() as cur:
        cur.execute(
            "select public.assert_active_business_profile(%(user_id)s::uuid, %(profile_id)s::uuid)",
            {"user_id": auth.user_id, "profile_id": profile_id},
        )

    current_balance_sql = _build_business_account_current_balance_sql(conn, active_only=True)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            select
              id,
              name,
              type,
              {current_balance_sql}
            from {accounts_relation}
            where user_id = %(user_id)s
              and profile_id = %(profile_id)s
              and is_active = true
              and type in ('cash', 'bank', 'merchant')
              and id = any(%(account_ids)s::uuid[])
            """,
            {
                "user_id": auth.user_id,
                "profile_id": profile_id,
                "account_ids": [from_account_id, to_account_id],
            },
        )
        account_rows = cur.fetchall() or []

    account_by_id = {str(row.get("id")): row for row in account_rows}
    from_account = account_by_id.get(from_account_id)
    to_account = account_by_id.get(to_account_id)
    if not from_account:
        raise ApiError(
            status_code=404,
            code="source_account_not_found",
            message="Source account not found for this business profile.",
        )
    if not to_account:
        raise ApiError(
            status_code=404,
            code="destination_account_not_found",
            message="Destination account not found for this business profile.",
        )

    from_balance = float(from_account.get("current_balance") or 0)
    if (from_balance - amount) < 0.0:
        available_amount = max(0.0, from_balance)
        account_name = str(from_account.get("name") or "account")
        raise ApiError(
            status_code=400,
            code="insufficient_funds",
            message=(
                f'Insufficient funds in "{account_name}". '
                f"Available amount is {available_amount:.2f}."
            ),
        )

    ledger_entries_relation = _business_ledger_entries_relation(conn)
    ledger_postings_relation = _business_ledger_postings_relation(conn)
    if not ledger_entries_relation or not ledger_postings_relation:
        raise ApiError(
            status_code=500,
            code="business_ledger_missing",
            message="Business ledger tables are missing. Run latest migrations.",
        )
    has_entry_metadata = _relation_has_column(conn, ledger_entries_relation, "metadata")

    metadata: dict[str, object] = {}
    if isinstance(payload.metadata, dict):
        metadata = {str(k): v for k, v in payload.metadata.items()}
    metadata.setdefault("operation", "account_transfer")
    metadata.setdefault("from_account_id", from_account_id)
    metadata.setdefault("to_account_id", to_account_id)
    transfer_txn_type = (
        "transfer" if _relation_supports_transfer_txn(conn, ledger_entries_relation) else "adjustment"
    )
    if transfer_txn_type != "transfer":
        metadata.setdefault("original_txn_type", "transfer")

    client_mutation_id = str(metadata.get("client_mutation_id") or "").strip()
    if has_entry_metadata and client_mutation_id:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                select id
                from {ledger_entries_relation}
                where user_id = %(user_id)s::uuid
                  and profile_id = %(profile_id)s::uuid
                  and coalesce(metadata ->> 'operation', '') = 'account_transfer'
                  and coalesce(metadata ->> 'client_mutation_id', '') = %(client_mutation_id)s
                limit 1
                """,
                {
                    "user_id": auth.user_id,
                    "profile_id": profile_id,
                    "client_mutation_id": client_mutation_id,
                },
            )
            existing_entry = cur.fetchone() or {}
        existing_entry_id = str(existing_entry.get("id") or "").strip()
        if existing_entry_id:
            return BusinessAccountTransferResponse(entry_id=UUID(existing_entry_id))

    description = str(payload.description or "").strip()
    if not description:
        description = (
            f"Transfer {str(from_account.get('name') or 'source')} -> "
            f"{str(to_account.get('name') or 'destination')}"
        )

    try:
        with conn.transaction():
            with conn.cursor() as cur:
                # Fail fast under account/ledger lock contention so client doesn't hang for 60s.
                cur.execute("set local lock_timeout = '5000ms'")
                cur.execute("set local statement_timeout = '15000ms'")
                cur.execute(
                    f"""
                    insert into {ledger_entries_relation} (
                      user_id,
                      profile_id,
                      txn_type,
                      amount,
                      date,
                      description,
                      account_id,
                      counterparty_id,
                      metadata
                    )
                    values (
                      %(user_id)s::uuid,
                      %(profile_id)s::uuid,
                      %(txn_type)s::text,
                      %(amount)s::numeric,
                      %(date)s::date,
                      %(description)s::text,
                      %(from_account_id)s::uuid,
                      null,
                      %(metadata)s::jsonb
                    )
                    returning id
                    """,
                    {
                        "user_id": auth.user_id,
                        "profile_id": profile_id,
                        "txn_type": transfer_txn_type,
                        "amount": amount,
                        "date": transfer_date.isoformat(),
                        "description": description,
                        "from_account_id": from_account_id,
                        "metadata": Jsonb(metadata),
                    },
                )
                row = cur.fetchone() or {}
                entry_id = str(row.get("id") or "").strip()
                if not entry_id:
                    raise ApiError(
                        status_code=500,
                        code="business_transfer_failed",
                        message="Failed to create transfer entry.",
                    )

                cur.execute(
                    f"""
                    insert into {ledger_postings_relation} (
                      entry_id,
                      user_id,
                      profile_id,
                      leg_type,
                      ref_id,
                      direction,
                      amount
                    )
                    values
                      (
                        %(entry_id)s::uuid,
                        %(user_id)s::uuid,
                        %(profile_id)s::uuid,
                        'account',
                        %(to_account_id)s::uuid,
                        'debit',
                        %(amount)s::numeric
                      ),
                      (
                        %(entry_id)s::uuid,
                        %(user_id)s::uuid,
                        %(profile_id)s::uuid,
                        'account',
                        %(from_account_id)s::uuid,
                        'credit',
                        %(amount)s::numeric
                      )
                    """,
                    {
                        "entry_id": entry_id,
                        "user_id": auth.user_id,
                        "profile_id": profile_id,
                        "to_account_id": to_account_id,
                        "from_account_id": from_account_id,
                        "amount": amount,
                    },
                )

                _assert_business_ledger_entry_is_balanced(
                    conn,
                    ledger_postings_relation=ledger_postings_relation,
                    entry_id=entry_id,
                )
    except PsycopgError as exc:
        if isinstance(exc, (LockNotAvailable, DeadlockDetected, QueryCanceled)):
            raise ApiError(
                status_code=409,
                code="business_transfer_busy",
                message=(
                    "Transfer is taking longer because another transaction is updating these "
                    "accounts. Please retry in a few seconds."
                ),
            ) from exc
        if isinstance(exc, CheckViolation) and "ledger_entries_txn_type_check" in str(exc):
            raise ApiError(
                status_code=500,
                code="business_transfer_txn_not_supported",
                message="Business transfer txn type is not supported by current schema. Apply latest migrations.",
            ) from exc
        message = str(exc).strip() or "Failed to transfer between business accounts."
        raise ApiError(
            status_code=400,
            code="business_transfer_failed",
            message=message,
        ) from exc

    _enqueue_business_ai_refresh(conn, user_id=auth.user_id, profile_id=profile_id)
    return BusinessAccountTransferResponse(entry_id=UUID(entry_id))


@router.patch("/accounts/{account_id}", response_model=BusinessAccountsResponse)
def patch_business_account(
    account_id: str,
    payload: BusinessAccountUpdateRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> BusinessAccountsResponse:
    apply_db_auth_context(conn, auth.user_id)
    accounts_relation = _business_accounts_relation(conn)
    if not accounts_relation:
        raise ApiError(
            status_code=500,
            code="business_accounts_table_missing",
            message="Business accounts table is not available.",
        )
    has_qr_image_url = _relation_has_column(conn, accounts_relation, "qr_image_url")
    has_overdraft_limit = _relation_has_column(conn, accounts_relation, "overdraft_limit")
    current_balance_sql = _build_business_account_current_balance_sql(conn)
    qr_update_sql = ", qr_image_url = %(qr_image_url)s" if has_qr_image_url else ""
    qr_return_sql = "qr_image_url," if has_qr_image_url else "null::text as qr_image_url,"
    with conn.cursor() as cur:
        cur.execute(
            f"""
            update {accounts_relation}
               set name = %(name)s,
                   institution_name = %(institution_name)s,
                   account_number = %(account_number)s{qr_update_sql},
                   updated_at = now()
             where id = %(account_id)s
               and user_id = %(user_id)s
               and profile_id = %(profile_id)s
               and type in ('bank', 'merchant')
            returning
              id, user_id, profile_id, name, type,
              institution_name, account_number, {qr_return_sql}
              opening_balance,
              {current_balance_sql},
              {"overdraft_limit" if has_overdraft_limit else "null::numeric as overdraft_limit"},
              is_active, created_at
            """,
            {
                "account_id": account_id,
                "user_id": auth.user_id,
                "profile_id": payload.profile_id,
                "name": payload.name.strip(),
                "institution_name": payload.institution_name.strip(),
                "account_number": (
                    payload.account_number.strip() if payload.account_number else None
                ),
                "qr_image_url": (
                    payload.qr_image_url.strip() if payload.qr_image_url else None
                ),
            },
        )
        row = cur.fetchone()
    if not row:
        raise ApiError(
            status_code=404,
            code="business_account_not_found",
            message="Business account not found.",
        )
    _enqueue_business_ai_refresh(
        conn,
        user_id=auth.user_id,
        profile_id=str(payload.profile_id or "").strip() or None,
    )
    return BusinessAccountsResponse(items=[row])


@router.delete("/accounts/{account_id}")
def deactivate_business_account(
    account_id: str,
    profile_id: str = Query(...),
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    apply_db_auth_context(conn, auth.user_id)
    with conn.cursor() as cur:
        cur.execute(
            """
            update public.accounts
               set is_active = false,
                   updated_at = now()
             where id = %(account_id)s
               and user_id = %(user_id)s
               and profile_id = %(profile_id)s
               and type in ('bank', 'merchant')
            returning id
            """,
            {
                "account_id": account_id,
                "user_id": auth.user_id,
                "profile_id": profile_id,
            },
        )
        row = cur.fetchone()
    if not row:
        raise ApiError(
            status_code=404,
            code="business_account_not_found",
            message="Business account not found.",
        )
    _enqueue_business_ai_refresh(conn, user_id=auth.user_id, profile_id=profile_id)
    return {"ok": True}


@router.get("/ledger/entries")
def get_business_ledger_entries(
    profile_id: str = Query(...),
    limit: int = Query(default=250, ge=1, le=1000),
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    apply_db_auth_context(conn, auth.user_id)
    ledger_entries_relation = _business_ledger_entries_relation(conn)
    if not ledger_entries_relation:
        return {"items": []}
    with conn.cursor() as cur:
        cur.execute(
            f"""
            select
              id, user_id, profile_id, txn_type, amount, date, description,
              account_id, counterparty_id, metadata, created_at
            from {ledger_entries_relation}
            where user_id = %(user_id)s
              and profile_id = %(profile_id)s
            order by date desc, created_at desc
            limit %(limit)s
            """,
            {"user_id": auth.user_id, "profile_id": profile_id, "limit": limit},
        )
        items = cur.fetchall() or []
    return {"items": items}


@router.get("/ledger/postings")
def get_business_ledger_postings_by_entry(
    profile_id: str = Query(...),
    entry_id: str = Query(...),
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    apply_db_auth_context(conn, auth.user_id)
    ledger_postings_relations = _existing_relations(
        conn,
        ["business.ledger_postings", "public.ledger_postings"],
    )
    if not ledger_postings_relations:
        return {"items": []}
    union_query = "\n            union\n".join(
        f"""
            select
              id, entry_id, user_id, profile_id, leg_type, ref_id, direction, amount, created_at
            from {relation}
            where user_id = %(user_id)s
              and profile_id = %(profile_id)s
              and entry_id = %(entry_id)s
        """
        for relation in ledger_postings_relations
    )
    with conn.cursor() as cur:
        cur.execute(
            f"""
            select *
            from (
              {union_query}
            ) postings
            order by created_at asc
            """,
            {
                "user_id": auth.user_id,
                "profile_id": profile_id,
                "entry_id": entry_id,
            },
        )
        items = cur.fetchall() or []
    return {"items": items}


@router.get("/products/added")
def get_business_product_added_entries(
    profile_id: str = Query(...),
    limit: int = Query(default=250, ge=1, le=1000),
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    apply_db_auth_context(conn, auth.user_id)
    products_relation = _business_products_relation(conn)
    if not products_relation:
        return {"items": []}
    with conn.cursor() as cur:
        cur.execute(
            f"""
            select id, user_id, profile_id, name, price, quantity, sku, created_at
            from {products_relation}
            where user_id = %(user_id)s
              and profile_id = %(profile_id)s
            order by created_at desc
            limit %(limit)s
            """,
            {"user_id": auth.user_id, "profile_id": profile_id, "limit": limit},
        )
        items = cur.fetchall() or []
    return {"items": items}


@router.get("/reference-maps")
def get_business_reference_maps(
    profile_id: str = Query(...),
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    apply_db_auth_context(conn, auth.user_id)
    accounts_relation = _business_accounts_relation(conn)
    customer_relation = _business_customers_relation(conn)
    products_relation = _business_products_relation(conn)
    suppliers_relation = _business_suppliers_relation(conn)
    with conn.cursor() as cur:
        if accounts_relation:
            cur.execute(
                f"""
                select id, name
                from {accounts_relation}
                where user_id = %(user_id)s
                  and profile_id = %(profile_id)s
                """,
                {"user_id": auth.user_id, "profile_id": profile_id},
            )
            accounts = cur.fetchall() or []
        else:
            accounts = []

        if customer_relation:
            cur.execute(
                f"""
                select id, name
                from {customer_relation}
                where user_id = %(user_id)s
                  and profile_id = %(profile_id)s
                """,
                {"user_id": auth.user_id, "profile_id": profile_id},
            )
            customers = cur.fetchall() or []
        else:
            customers = []

        if products_relation:
            cur.execute(
                f"""
                select id, name
                from {products_relation}
                where user_id = %(user_id)s
                  and profile_id = %(profile_id)s
                """,
                {"user_id": auth.user_id, "profile_id": profile_id},
            )
            products = cur.fetchall() or []
        else:
            products = []

        if suppliers_relation:
            cur.execute(
                f"""
                select id, name
                from {suppliers_relation}
                where user_id = %(user_id)s
                  and profile_id = %(profile_id)s
                """,
                {"user_id": auth.user_id, "profile_id": profile_id},
            )
            suppliers = cur.fetchall() or []
        else:
            suppliers = []

    return {
        "accounts": accounts,
        "customers": customers,
        "products": products,
        "suppliers": suppliers,
    }


@router.get("/invoices/product-names")
def get_invoice_product_names_by_invoice_id(
    profile_id: str = Query(...),
    limit: int = Query(default=600, ge=1, le=2000),
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    apply_db_auth_context(conn, auth.user_id)
    invoices_relation = _business_invoices_relation(conn)
    invoice_items_relation = _business_invoice_items_relation(conn)
    products_relation = _business_products_relation(conn)
    if not invoices_relation or not invoice_items_relation:
        return {"items": []}
    if not products_relation:
        products_relation = "(select null::uuid as id, null::text as name) as products_empty"
    with conn.cursor() as cur:
        cur.execute(
            f"""
            select
              i.id,
              coalesce(
                json_agg(
                  json_build_object(
                    'product_name_snapshot', ii.product_name_snapshot,
                    'product_name', p.name
                  )
                  order by ii.created_at asc
                ) filter (where ii.id is not null),
                '[]'::json
              ) as items
            from {invoices_relation} i
            left join {invoice_items_relation} ii on ii.invoice_id = i.id
            left join {products_relation} p on p.id = ii.product_id
            where i.user_id = %(user_id)s
              and i.profile_id = %(profile_id)s
            group by i.id, i.date, i.created_at
            order by i.date desc, i.created_at desc
            limit %(limit)s
            """,
            {"user_id": auth.user_id, "profile_id": profile_id, "limit": limit},
        )
        items = cur.fetchall() or []
    return {"items": items}


@router.get("/accounts/balance-state")
def get_business_accounts_balance_state(
    profile_id: str = Query(...),
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    apply_db_auth_context(conn, auth.user_id)
    ledger_postings_relation = _business_ledger_postings_relation(conn)
    ledger_entries_relation = _business_ledger_entries_relation(conn)
    with conn.cursor() as cur:
        if ledger_postings_relation:
            cur.execute(
                f"""
                select ref_id, direction, amount
                from {ledger_postings_relation}
                where user_id = %(user_id)s
                  and profile_id = %(profile_id)s
                  and leg_type = 'account'
                  and ref_id is not null
                order by created_at asc
                """,
                {"user_id": auth.user_id, "profile_id": profile_id},
            )
            posting_rows = cur.fetchall() or []
        else:
            posting_rows = []

        if ledger_entries_relation:
            cur.execute(
                f"""
                select account_id
                from {ledger_entries_relation}
                where user_id = %(user_id)s
                  and profile_id = %(profile_id)s
                  and txn_type = 'opening_account'
                  and account_id is not null
                """,
                {"user_id": auth.user_id, "profile_id": profile_id},
            )
            opening_rows = cur.fetchall() or []
        else:
            opening_rows = []

    return {
        "posting_rows": posting_rows,
        "opening_account_ids": [str(row.get("account_id")) for row in opening_rows if row.get("account_id")],
    }


@router.get("/payables/raw")
def get_business_payables_raw(
    profile_id: str = Query(...),
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    apply_db_auth_context(conn, auth.user_id)
    ledger_postings_relation = _business_ledger_postings_relation(conn)
    ledger_entries_relation = _business_ledger_entries_relation(conn)
    suppliers_relation = _business_suppliers_relation(conn)
    with conn.cursor() as cur:
        if ledger_postings_relation:
            cur.execute(
                f"""
                select id, direction, amount
                from {ledger_postings_relation}
                where user_id = %(user_id)s
                  and profile_id = %(profile_id)s
                  and leg_type = 'payable'
                """,
                {"user_id": auth.user_id, "profile_id": profile_id},
            )
            payable_rows = cur.fetchall() or []
        else:
            payable_rows = []

        if ledger_entries_relation:
            cur.execute(
                f"""
                select id, amount, date, description, metadata
                from {ledger_entries_relation}
                where user_id = %(user_id)s
                  and profile_id = %(profile_id)s
                  and txn_type = 'inventory_in'
                """,
                {"user_id": auth.user_id, "profile_id": profile_id},
            )
            credit_entries = cur.fetchall() or []

            cur.execute(
                f"""
                select id, amount, date, description, metadata
                from {ledger_entries_relation}
                where user_id = %(user_id)s
                  and profile_id = %(profile_id)s
                  and txn_type = 'adjustment'
                """,
                {"user_id": auth.user_id, "profile_id": profile_id},
            )
            repayment_entries = cur.fetchall() or []
        else:
            credit_entries = []
            repayment_entries = []

        if suppliers_relation:
            cur.execute(
                f"""
                select id, name
                from {suppliers_relation}
                where user_id = %(user_id)s
                  and profile_id = %(profile_id)s
                """,
                {"user_id": auth.user_id, "profile_id": profile_id},
            )
            suppliers = cur.fetchall() or []
        else:
            suppliers = []

    return {
        "payable_rows": payable_rows,
        "credit_entries": credit_entries,
        "repayment_entries": repayment_entries,
        "suppliers": suppliers,
    }


@router.get("/customers")
def list_business_customers(
    profile_id: str = Query(...),
    include_inactive: bool = Query(default=False),
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    apply_db_auth_context(conn, auth.user_id)
    relation = _business_customers_relation(conn)
    if not relation:
        return {"items": []}
    status_sql = "" if include_inactive else "and is_active = true"
    with conn.cursor() as cur:
        cur.execute(
            f"""
            select *
            from {relation}
            where user_id = %(user_id)s
              and profile_id = %(profile_id)s
              {status_sql}
            order by created_at asc
            """,
            {"user_id": auth.user_id, "profile_id": profile_id},
        )
        items = cur.fetchall() or []
    return {"items": items}


@router.get("/suppliers")
def list_business_suppliers(
    profile_id: str = Query(...),
    include_inactive: bool = Query(default=False),
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    apply_db_auth_context(conn, auth.user_id)
    items = _list_business_suppliers_direct(
        conn,
        auth_user_id=auth.user_id,
        profile_id=profile_id,
        include_inactive=include_inactive,
    )
    safe_items = _to_json_safe(items)
    return {"items": safe_items if isinstance(safe_items, list) else []}


@router.get("/customer-categories")
def list_business_customer_categories(
    profile_id: str = Query(...),
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    apply_db_auth_context(conn, auth.user_id)
    relation = _legacy_category_relation_for_domain(conn, "customer")
    if not relation:
        return {"items": []}
    has_profile_id = _relation_has_column(conn, relation, "profile_id")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            select *
            from {relation}
            where user_id = %(user_id)s::uuid
              {'and profile_id = %(profile_id)s::uuid' if has_profile_id else ''}
            order by created_at asc
            """,
            {"user_id": auth.user_id, "profile_id": profile_id},
        )
        items = cur.fetchall() or []
    return {"items": items}


@router.get("/categories")
def list_business_categories(
    profile_id: str = Query(...),
    domain: str = Query(...),
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    apply_db_auth_context(conn, auth.user_id)
    normalized_domain = _validate_business_category_domain(domain)

    if _force_legacy_category_domain(normalized_domain) or not _has_unified_business_categories(conn):
        relation = _legacy_category_relation_for_domain(conn, normalized_domain)
        if not relation:
            return {"items": []}
        has_profile_id = _relation_has_column(conn, relation, "profile_id")
        has_parent_id = _relation_has_column(conn, relation, "parent_id")
        has_type = _relation_has_column(conn, relation, "type")
        has_is_active = _relation_has_column(conn, relation, "is_active")
        has_updated_at = _relation_has_column(conn, relation, "updated_at")
        where_parts = ["user_id = %(user_id)s::uuid"]
        if has_profile_id:
            where_parts.append("profile_id = %(profile_id)s::uuid")
        if has_type and normalized_domain in {"income", "expense"}:
            where_parts.append("type = %(domain)s::text")
        if has_is_active:
            where_parts.append("is_active = true")
        where_sql = " and ".join(where_parts)
        select_domain = "type::text" if has_type else "%(domain)s::text"
        select_parent = "parent_id" if has_parent_id else "null::uuid"
        select_active = "is_active" if has_is_active else "true"
        select_updated = "updated_at" if has_updated_at else "created_at"
        with conn.cursor() as cur:
            cur.execute(
                f"""
                select
                  id, user_id,
                  {'profile_id' if has_profile_id else '%(profile_id)s::uuid as profile_id'},
                  {select_domain} as domain,
                  name,
                  {select_parent} as parent_id,
                  {select_active} as is_active,
                  created_at,
                  {select_updated} as updated_at
                from {relation}
                where {where_sql}
                order by created_at asc
                """,
                {
                    "user_id": auth.user_id,
                    "profile_id": profile_id,
                    "domain": normalized_domain,
                },
            )
            return {"items": cur.fetchall() or []}

    with conn.cursor() as cur:
        cur.execute(
            """
            select
              id, user_id, profile_id, domain, name, parent_id, is_active, created_at, updated_at
            from public.business_categories
            where user_id = %(user_id)s
              and profile_id = %(profile_id)s
              and domain = %(domain)s
              and is_active = true
            order by created_at asc
            """,
            {
                "user_id": auth.user_id,
                "profile_id": profile_id,
                "domain": normalized_domain,
            },
        )
        items = cur.fetchall() or []
    return {"items": items}


@router.post("/categories")
def create_business_category(
    payload: dict,
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    profile_id = str(payload.get("profile_id") or "").strip()
    name = _normalize_business_category_display_name(str(payload.get("name") or ""))
    parent_id = str(payload.get("parent_id") or "").strip() or None
    domain = _validate_business_category_domain(str(payload.get("domain") or ""))

    if not profile_id:
        raise ApiError(status_code=400, code="profile_required", message="profile_id is required.")
    if not name:
        raise ApiError(status_code=400, code="name_required", message="name is required.")

    apply_db_auth_context(conn, auth.user_id)

    if _force_legacy_category_domain(domain) or not _has_unified_business_categories(conn):
        relation = _legacy_category_relation_for_domain(conn, domain)
        if not relation:
            raise ApiError(
                status_code=400,
                code="category_domain_not_available",
                message=f"{domain.capitalize()} category table is not available. Run latest migrations.",
            )
        has_profile_id = _relation_has_column(conn, relation, "profile_id")
        has_parent_id = _relation_has_column(conn, relation, "parent_id")
        has_type = _relation_has_column(conn, relation, "type")
        has_is_active = _relation_has_column(conn, relation, "is_active")
        has_updated_at = _relation_has_column(conn, relation, "updated_at")
        existing_items = _list_business_category_candidates(
            conn,
            auth=auth,
            profile_id=profile_id,
            domain=domain,
            parent_id=parent_id,
            relation=relation,
            legacy_has_profile_id=has_profile_id,
            legacy_has_parent_id=has_parent_id,
            legacy_has_type=has_type,
            legacy_has_is_active=has_is_active,
            legacy_has_updated_at=has_updated_at,
        )
        matched_item, resolution = _find_strong_matching_business_category(existing_items, name=name)
        if matched_item:
            return {
                "item": matched_item,
                "existing": True,
                "resolution": f"matched_{resolution}",
            }
        columns = ["user_id", "name"]
        values = ["%(user_id)s::uuid", "%(name)s::text"]
        if has_profile_id:
            columns.append("profile_id")
            values.append("%(profile_id)s::uuid")
        if has_type:
            columns.append("type")
            values.append("%(domain)s::text")
        if has_parent_id:
            columns.append("parent_id")
            values.append("%(parent_id)s::uuid")
        with conn.cursor() as cur:
            cur.execute(
                f"""
                insert into {relation} ({", ".join(columns)})
                values ({", ".join(values)})
                returning
                  id,
                  user_id,
                  {'profile_id' if has_profile_id else '%(profile_id)s::uuid as profile_id'},
                  {'type::text' if has_type else '%(domain)s::text'} as domain,
                  name,
                  {'parent_id' if has_parent_id else 'null::uuid'} as parent_id,
                  {'is_active' if has_is_active else 'true'} as is_active,
                  created_at,
                  {'updated_at' if has_updated_at else 'created_at'} as updated_at
                """,
                {
                    "user_id": auth.user_id,
                    "profile_id": profile_id,
                    "name": name,
                    "domain": domain,
                    "parent_id": parent_id,
                },
            )
            item = cur.fetchone()
            _enqueue_business_ai_refresh(conn, user_id=auth.user_id, profile_id=profile_id)
            return {"item": item}

    existing_items = _list_business_category_candidates(
        conn,
        auth=auth,
        profile_id=profile_id,
        domain=domain,
        parent_id=parent_id,
    )
    matched_item, resolution = _find_strong_matching_business_category(existing_items, name=name)
    if matched_item:
        return {
            "item": matched_item,
            "existing": True,
            "resolution": f"matched_{resolution}",
        }

    with conn.cursor() as cur:
        cur.execute(
            """
            insert into public.business_categories (
              user_id, profile_id, domain, name, parent_id
            )
            values (
              %(user_id)s::uuid, %(profile_id)s::uuid, %(domain)s::text, %(name)s::text, %(parent_id)s::uuid
            )
            returning
              id, user_id, profile_id, domain, name, parent_id, is_active, created_at, updated_at
            """,
            {
                "user_id": auth.user_id,
                "profile_id": profile_id,
                "domain": domain,
                "name": name,
                "parent_id": parent_id,
            },
        )
        item = cur.fetchone()
    _enqueue_business_ai_refresh(conn, user_id=auth.user_id, profile_id=profile_id)
    return {"item": item}


@router.patch("/categories/{category_id}")
def update_business_category(
    category_id: str,
    payload: dict,
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    profile_id = str(payload.get("profile_id") or "").strip()
    name = _normalize_business_category_display_name(str(payload.get("name") or ""))
    parent_id = str(payload.get("parent_id") or "").strip() or None

    if not profile_id:
        raise ApiError(status_code=400, code="profile_required", message="profile_id is required.")
    if not name:
        raise ApiError(status_code=400, code="name_required", message="name is required.")

    apply_db_auth_context(conn, auth.user_id)

    for legacy_domain in ("product", "customer", "supplier", "income", "expense"):
        relation = _legacy_category_relation_for_domain(conn, legacy_domain)
        if not relation:
            continue
        has_profile_id = _relation_has_column(conn, relation, "profile_id")
        has_parent_id = _relation_has_column(conn, relation, "parent_id")
        has_type = _relation_has_column(conn, relation, "type")
        has_is_active = _relation_has_column(conn, relation, "is_active")
        has_updated_at = _relation_has_column(conn, relation, "updated_at")
        current_where_parts = [
            "id = %(category_id)s::uuid",
            "user_id = %(user_id)s::uuid",
        ]
        if has_profile_id:
            current_where_parts.append("profile_id = %(profile_id)s::uuid")
        if has_type:
            current_where_parts.append("type = %(domain)s::text")
        if has_is_active:
            current_where_parts.append("is_active = true")
        with conn.cursor() as cur:
            cur.execute(
                f"""
                select id
                from {relation}
                where {' and '.join(current_where_parts)}
                """,
                {
                    "category_id": category_id,
                    "user_id": auth.user_id,
                    "profile_id": profile_id,
                    "domain": legacy_domain,
                },
            )
            current_item = cur.fetchone()
        if not current_item:
            continue

        existing_items = _list_business_category_candidates(
            conn,
            auth=auth,
            profile_id=profile_id,
            domain=legacy_domain,
            parent_id=parent_id,
            exclude_category_id=category_id,
            relation=relation,
            legacy_has_profile_id=has_profile_id,
            legacy_has_parent_id=has_parent_id,
            legacy_has_type=has_type,
            legacy_has_is_active=has_is_active,
            legacy_has_updated_at=has_updated_at,
        )
        matched_item, resolution = _find_strong_matching_business_category(existing_items, name=name)
        if matched_item:
            raise ApiError(
                status_code=409,
                code="category_name_resolves_to_existing",
                message=(
                    f'Category name resolves to existing category "{matched_item.get("name") or ""}".'
                ),
                details={
                    "existing_category_id": str(matched_item.get("id") or ""),
                    "existing_category_name": str(matched_item.get("name") or ""),
                    "resolution": resolution,
                },
            )
        where_parts = [
            "id = %(category_id)s::uuid",
            "user_id = %(user_id)s::uuid",
        ]
        if has_profile_id:
            where_parts.append("profile_id = %(profile_id)s::uuid")
        if has_type:
            where_parts.append("type = %(domain)s::text")
        where_sql = " and ".join(where_parts)
        set_parts = ["name = %(name)s::text"]
        if has_parent_id:
            set_parts.append("parent_id = %(parent_id)s::uuid")
        if has_updated_at:
            set_parts.append("updated_at = now()")
        set_sql = ", ".join(set_parts)
        with conn.cursor() as cur:
            cur.execute(
                f"""
                update {relation}
                   set {set_sql}
                 where {where_sql}
                returning
                  id,
                  user_id,
                  {'profile_id' if has_profile_id else '%(profile_id)s::uuid as profile_id'},
                  {'type::text' if has_type else '%(domain)s::text'} as domain,
                  name,
                  {'parent_id' if has_parent_id else 'null::uuid'} as parent_id,
                  {'is_active' if has_is_active else 'true'} as is_active,
                  created_at,
                  {'updated_at' if has_updated_at else 'created_at'} as updated_at
                """,
                {
                    "category_id": category_id,
                    "user_id": auth.user_id,
                    "profile_id": profile_id,
                    "name": name,
                    "parent_id": parent_id,
                    "domain": legacy_domain,
                },
            )
            item = cur.fetchone()
        if item:
            _enqueue_business_ai_refresh(conn, user_id=auth.user_id, profile_id=profile_id)
            return {"item": item}

    if not _has_unified_business_categories(conn):
        raise ApiError(
            status_code=404,
            code="category_not_found",
            message="Business category not found.",
        )

    with conn.cursor() as cur:
        cur.execute(
            """
            select id, user_id, profile_id, domain, name, parent_id, is_active, created_at, updated_at
            from public.business_categories
            where id = %(category_id)s::uuid
              and user_id = %(user_id)s::uuid
              and profile_id = %(profile_id)s::uuid
              and is_active = true
            """,
            {
                "category_id": category_id,
                "user_id": auth.user_id,
                "profile_id": profile_id,
            },
        )
        existing_category = cur.fetchone()

    if not existing_category:
        raise ApiError(
            status_code=404,
            code="category_not_found",
            message="Business category not found.",
        )

    existing_items = _list_business_category_candidates(
        conn,
        auth=auth,
        profile_id=profile_id,
        domain=str(existing_category.get("domain") or ""),
        parent_id=parent_id,
        exclude_category_id=category_id,
    )
    matched_item, resolution = _find_strong_matching_business_category(existing_items, name=name)
    if matched_item:
        raise ApiError(
            status_code=409,
            code="category_name_resolves_to_existing",
            message=(
                f'Category name resolves to existing category "{matched_item.get("name") or ""}".'
            ),
            details={
                "existing_category_id": str(matched_item.get("id") or ""),
                "existing_category_name": str(matched_item.get("name") or ""),
                "resolution": resolution,
            },
        )

    with conn.cursor() as cur:
        cur.execute(
            """
            update public.business_categories
               set name = %(name)s::text,
                   parent_id = %(parent_id)s::uuid,
                   updated_at = now()
             where id = %(category_id)s::uuid
               and user_id = %(user_id)s::uuid
               and profile_id = %(profile_id)s::uuid
               and is_active = true
            returning
              id, user_id, profile_id, domain, name, parent_id, is_active, created_at, updated_at
            """,
            {
                "category_id": category_id,
                "user_id": auth.user_id,
                "profile_id": profile_id,
                "name": name,
                "parent_id": parent_id,
            },
        )
        item = cur.fetchone()

    if not item:
        raise ApiError(
            status_code=404,
            code="category_not_found",
            message="Business category not found.",
        )
    _enqueue_business_ai_refresh(conn, user_id=auth.user_id, profile_id=profile_id)
    return {"item": item}


@router.delete("/categories/{category_id}")
def deactivate_business_category(
    category_id: str,
    profile_id: str = Query(...),
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    apply_db_auth_context(conn, auth.user_id)

    for legacy_domain in ("product", "customer", "supplier", "income", "expense"):
        relation = _legacy_category_relation_for_domain(conn, legacy_domain)
        if not relation:
            continue
        has_profile_id = _relation_has_column(conn, relation, "profile_id")
        has_type = _relation_has_column(conn, relation, "type")
        has_is_active = _relation_has_column(conn, relation, "is_active")
        where_parts = [
            "id = %(category_id)s::uuid",
            "user_id = %(user_id)s::uuid",
        ]
        if has_profile_id:
            where_parts.append("profile_id = %(profile_id)s::uuid")
        if has_type:
            where_parts.append("type = %(domain)s::text")
        where_sql = " and ".join(where_parts)

        with conn.cursor() as cur:
            if has_is_active:
                cur.execute(
                    f"""
                    update {relation}
                       set is_active = false{", updated_at = now()" if _relation_has_column(conn, relation, "updated_at") else ""}
                     where {where_sql}
                       and is_active = true
                    returning id
                    """,
                    {
                        "category_id": category_id,
                        "user_id": auth.user_id,
                        "profile_id": profile_id,
                        "domain": legacy_domain,
                    },
                )
            else:
                cur.execute(
                    f"""
                    delete from {relation}
                     where {where_sql}
                    returning id
                    """,
                    {
                        "category_id": category_id,
                        "user_id": auth.user_id,
                        "profile_id": profile_id,
                        "domain": legacy_domain,
                    },
                )
            deleted = cur.fetchone()
        if deleted:
            _enqueue_business_ai_refresh(conn, user_id=auth.user_id, profile_id=profile_id)
            return {"ok": True}

    if not _has_unified_business_categories(conn):
        raise ApiError(
            status_code=404,
            code="category_not_found",
            message="Business category not found.",
        )

    with conn.cursor() as cur:
        # First, move all child categories to top level (parent_id = NULL)
        cur.execute(
            """
            update public.business_categories
               set parent_id = null,
                   updated_at = now()
             where user_id = %(user_id)s::uuid
               and profile_id = %(profile_id)s::uuid
               and parent_id = %(category_id)s::uuid
               and is_active = true
            """,
            {
                "user_id": auth.user_id,
                "profile_id": profile_id,
                "category_id": category_id,
            },
        )

        # Then, soft delete the parent category
        cur.execute(
            """
            update public.business_categories
               set is_active = false,
                   updated_at = now()
             where id = %(category_id)s::uuid
               and user_id = %(user_id)s::uuid
               and profile_id = %(profile_id)s::uuid
               and is_active = true
            returning id
            """,
            {
                "category_id": category_id,
                "user_id": auth.user_id,
                "profile_id": profile_id,
            },
        )
        item = cur.fetchone()
    if not item:
        raise ApiError(
            status_code=404,
            code="category_not_found",
            message="Business category not found.",
        )
    _enqueue_business_ai_refresh(conn, user_id=auth.user_id, profile_id=profile_id)
    return {"ok": True}


@router.get("/account-categories")
def list_business_account_categories() -> dict:
    return {
        "items": [
            {"id": "cash", "name": "Cash"},
            {"id": "bank", "name": "Bank"},
            {"id": "merchant", "name": "Merchant"},
        ]
    }


@router.post("/customer-categories")
def create_business_customer_category(
    payload: dict,
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    profile_id = str(payload.get("profile_id") or "").strip()
    name = str(payload.get("name") or "").strip()
    if not profile_id:
        raise ApiError(status_code=400, code="profile_required", message="profile_id is required.")
    if not name:
        raise ApiError(status_code=400, code="name_required", message="name is required.")

    apply_db_auth_context(conn, auth.user_id)
    relation = _legacy_category_relation_for_domain(conn, "customer")
    if not relation:
        raise ApiError(
            status_code=400,
            code="customer_category_table_missing",
            message="Customer category table is not available. Run latest migrations.",
        )
    has_profile_id = _relation_has_column(conn, relation, "profile_id")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            insert into {relation} (user_id, {'profile_id,' if has_profile_id else ''} name)
            values (%(user_id)s::uuid, {'%(profile_id)s::uuid,' if has_profile_id else ''} %(name)s::text)
            returning *
            """,
            {"user_id": auth.user_id, "profile_id": profile_id, "name": name},
        )
        item = cur.fetchone()
    if not item:
        raise ApiError(
            status_code=500,
            code="create_failed",
            message="Customer category create failed.",
        )
    _enqueue_business_ai_refresh(conn, user_id=auth.user_id, profile_id=profile_id)
    return {"item": item}


@router.post("/customers")
def create_business_customer(
    payload: dict,
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    profile_id = str(payload.get("profile_id") or "").strip()
    if not profile_id:
        raise ApiError(status_code=400, code="profile_required", message="profile_id is required.")
    apply_db_auth_context(conn, auth.user_id)

    existing_receipt = _load_mutation_receipt(
        conn,
        user_id=auth.user_id,
        profile_id=profile_id,
        mutation_type="business.customer-create",
        idempotency_key=payload.get("idempotency_key"),
    )
    if existing_receipt:
        return existing_receipt

    relation = _business_customers_relation(conn)
    if not relation:
        raise ApiError(status_code=500, code="customers_table_missing", message="Customer table is not available.")
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ApiError(status_code=400, code="name_required", message="name is required.")
    has_phone = _relation_has_column(conn, relation, "phone")
    has_address = _relation_has_column(conn, relation, "address")
    has_category_id = _relation_has_column(conn, relation, "category_id")
    has_credit_limit = _relation_has_column(conn, relation, "credit_limit")
    has_opening_balance = _relation_has_column(conn, relation, "opening_balance")
    has_opening_balance_type = _relation_has_column(conn, relation, "opening_balance_type")
    has_reminder_date = _relation_has_column(conn, relation, "reminder_date")
    opening_balance = float(payload.get("opening_balance") or 0)
    opening_balance_type = str(payload.get("opening_balance_type") or "receivable").strip().lower()
    if opening_balance_type not in {"receivable", "payable"}:
        opening_balance_type = "receivable"
    if opening_balance < 0:
        raise ApiError(status_code=400, code="invalid_opening_balance", message="opening_balance must be zero or greater.")
    reminder_date = payload.get("reminder_date")
    item = None
    try:
        with conn.cursor() as cur:
            fields = ["user_id", "profile_id", "name"]
            values = ["%(user_id)s", "%(profile_id)s", "%(name)s"]
            bind: dict[str, object] = {
                "user_id": auth.user_id,
                "profile_id": profile_id,
                "name": name,
            }
            if has_phone:
                fields.append("phone")
                values.append("%(phone)s")
                bind["phone"] = (str(payload.get("phone")).strip() if payload.get("phone") else None)
            if has_address:
                fields.append("address")
                values.append("%(address)s")
                bind["address"] = (str(payload.get("address")).strip() if payload.get("address") else None)
            if has_category_id:
                fields.append("category_id")
                values.append("%(category_id)s")
                bind["category_id"] = payload.get("category_id")
            if has_credit_limit:
                fields.append("credit_limit")
                values.append("%(credit_limit)s")
                bind["credit_limit"] = payload.get("credit_limit", 0)
            if has_opening_balance:
                fields.append("opening_balance")
                values.append("%(opening_balance)s")
                bind["opening_balance"] = opening_balance
            if has_opening_balance_type:
                fields.append("opening_balance_type")
                values.append("%(opening_balance_type)s")
                bind["opening_balance_type"] = opening_balance_type
            if has_reminder_date:
                fields.append("reminder_date")
                values.append("%(reminder_date)s")
                bind["reminder_date"] = reminder_date

            # Prevent hidden duplicates from soft-deleted rows and support re-adding same customer name.
            cur.execute(
                f"""
                select *
                from {relation}
                where user_id = %(user_id)s
                  and profile_id = %(profile_id)s
                  and lower(trim(name)) = lower(trim(%(name)s))
                order by is_active desc, created_at asc
                limit 1
                """,
                {"user_id": auth.user_id, "profile_id": profile_id, "name": name},
            )
            existing_item = cur.fetchone()
            if existing_item:
                was_active = bool(existing_item.get("is_active"))
                set_parts = ["updated_at = now()", "name = %(name)s"]
                if not was_active:
                    set_parts.insert(0, "is_active = true")
                update_bind: dict[str, object] = {
                    "customer_id": existing_item.get("id"),
                    "user_id": auth.user_id,
                    "profile_id": profile_id,
                    "name": name,
                }
                if has_phone:
                    set_parts.append("phone = %(phone)s")
                    update_bind["phone"] = bind.get("phone")
                if has_address:
                    set_parts.append("address = %(address)s")
                    update_bind["address"] = bind.get("address")
                if has_category_id:
                    set_parts.append("category_id = %(category_id)s")
                    update_bind["category_id"] = bind.get("category_id")
                if has_credit_limit:
                    set_parts.append("credit_limit = %(credit_limit)s")
                    update_bind["credit_limit"] = bind.get("credit_limit", 0)
                if has_opening_balance:
                    set_parts.append("opening_balance = %(opening_balance)s")
                    update_bind["opening_balance"] = bind.get("opening_balance", 0)
                if has_opening_balance_type:
                    set_parts.append("opening_balance_type = %(opening_balance_type)s")
                    update_bind["opening_balance_type"] = bind.get("opening_balance_type", "receivable")
                if has_reminder_date:
                    set_parts.append("reminder_date = %(reminder_date)s")
                    update_bind["reminder_date"] = bind.get("reminder_date")

                cur.execute(
                    f"""
                    update {relation}
                       set {", ".join(set_parts)}
                     where id = %(customer_id)s
                       and user_id = %(user_id)s
                       and profile_id = %(profile_id)s
                    returning *
                    """,
                    update_bind,
                )
                item = cur.fetchone()
                _enqueue_business_ai_refresh(conn, user_id=auth.user_id, profile_id=profile_id)
                response = {"item": item, "reactivated": (not was_active), "existing": was_active}
                _store_mutation_receipt(
                    conn,
                    user_id=auth.user_id,
                    profile_id=profile_id,
                    mutation_type="business.customer-create",
                    idempotency_key=payload.get("idempotency_key"),
                    response_payload=jsonable_encoder(response),
                )
                return response

            cur.execute(
                f"""
                insert into {relation} (
                  {", ".join(fields)}
                ) values (
                  {", ".join(values)}
                )
                returning *
                """,
                bind,
            )
            item = cur.fetchone()
            if item and opening_balance > 0:
                posting_amount = round(opening_balance, 2)
                ledger_entries_relation = _first_existing_relation(conn, ["business.ledger_entries"])
                ledger_postings_relation = _first_existing_relation(conn, ["business.ledger_postings"])
                if not ledger_entries_relation or not ledger_postings_relation:
                    raise ApiError(
                        status_code=500,
                        code="business_ledger_missing",
                        message="Business ledger tables are missing. Run latest migrations.",
                    )

                cur.execute(
                    f"""
                    insert into {ledger_entries_relation} (
                      user_id, profile_id, txn_type, amount, date, description, metadata
                    ) values (
                      %(user_id)s, %(profile_id)s, 'adjustment', %(amount)s, current_date, %(description)s, %(metadata)s
                    ) returning id
                    """,
                    {
                        "user_id": auth.user_id,
                        "profile_id": profile_id,
                        "amount": posting_amount,
                        "description": f"Opening customer balance | {item.get('name') or ''} | {opening_balance_type}",
                        "metadata": Jsonb(
                            {
                                "source": "customer_opening",
                                "customer_id": str(item.get("id")),
                                "opening_balance_type": opening_balance_type,
                            }
                        ),
                    },
                )
                entry_row = cur.fetchone() or {}
                entry_id = entry_row.get("id")
                if entry_id:
                    if opening_balance_type == "receivable":
                        cur.execute(
                            f"""
                            insert into {ledger_postings_relation} (entry_id, user_id, profile_id, leg_type, ref_id, direction, amount)
                            values
                              (%(entry_id)s, %(user_id)s, %(profile_id)s, 'receivable', %(customer_id)s, 'debit', %(amount)s),
                              (%(entry_id)s, %(user_id)s, %(profile_id)s, 'opening_equity', null, 'credit', %(amount)s)
                            """,
                            {
                                "entry_id": entry_id,
                                "user_id": auth.user_id,
                                "profile_id": profile_id,
                                "customer_id": item.get("id"),
                                "amount": posting_amount,
                            },
                        )
                    else:
                        cur.execute(
                            f"""
                            insert into {ledger_postings_relation} (entry_id, user_id, profile_id, leg_type, ref_id, direction, amount)
                            values
                              (%(entry_id)s, %(user_id)s, %(profile_id)s, 'payable', %(customer_id)s, 'credit', %(amount)s),
                              (%(entry_id)s, %(user_id)s, %(profile_id)s, 'opening_equity', null, 'debit', %(amount)s)
                            """,
                            {
                                "entry_id": entry_id,
                                "user_id": auth.user_id,
                                "profile_id": profile_id,
                                "customer_id": item.get("id"),
                                "amount": posting_amount,
                            },
                        )
                    _assert_business_ledger_entry_is_balanced(
                        conn,
                        ledger_postings_relation=ledger_postings_relation,
                        entry_id=str(entry_id),
                    )
    except UniqueViolation as exc:
        # If a concurrent request inserted the same customer name first, treat this as idempotent create.
        with conn.cursor() as cur:
            cur.execute(
                f"""
                select *
                from {relation}
                where user_id = %(user_id)s
                  and profile_id = %(profile_id)s
                  and lower(trim(name)) = lower(trim(%(name)s))
                order by is_active desc, created_at asc
                limit 1
                """,
                {"user_id": auth.user_id, "profile_id": profile_id, "name": name},
            )
            existing_item = cur.fetchone()
        if existing_item:
            _enqueue_business_ai_refresh(conn, user_id=auth.user_id, profile_id=profile_id)
            response = {"item": existing_item, "existing": True}
            _store_mutation_receipt(
                conn,
                user_id=auth.user_id,
                profile_id=profile_id,
                mutation_type="business.customer-create",
                idempotency_key=payload.get("idempotency_key"),
                response_payload=jsonable_encoder(response),
            )
            return response
        raise ApiError(
            status_code=409,
            code="customer_name_exists",
            message="Customer with this name already exists in this profile.",
        ) from exc
    except PsycopgError as exc:
        message = str(exc).strip()
        if "address" in message.lower() and "column" in message.lower():
            raise ApiError(
                status_code=500,
                code="customers_schema_mismatch",
                message="Customers address column is missing. Run latest migrations.",
            ) from exc
        raise ApiError(
            status_code=400,
            code="business_customer_create_failed",
            message=message or "Failed to create customer.",
        ) from exc
    if not item:
        raise ApiError(status_code=500, code="create_failed", message="Customer create failed.")
    _enqueue_business_ai_refresh(conn, user_id=auth.user_id, profile_id=profile_id)
    response = {"item": item}
    _store_mutation_receipt(
        conn,
        user_id=auth.user_id,
        profile_id=profile_id,
        mutation_type="business.customer-create",
        idempotency_key=payload.get("idempotency_key"),
        response_payload=jsonable_encoder(response),
    )
    return response


@router.patch("/customers/{customer_id}")
def update_business_customer(
    customer_id: str,
    payload: dict,
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    profile_id = str(payload.get("profile_id") or "").strip()
    if not profile_id:
        raise ApiError(status_code=400, code="profile_required", message="profile_id is required.")
    apply_db_auth_context(conn, auth.user_id)
    relation = _business_customers_relation(conn)
    if not relation:
        raise ApiError(status_code=500, code="customers_table_missing", message="Customer table is not available.")
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ApiError(status_code=400, code="name_required", message="name is required.")
    has_phone = _relation_has_column(conn, relation, "phone")
    has_address = _relation_has_column(conn, relation, "address")
    has_category_id = _relation_has_column(conn, relation, "category_id")
    has_opening_balance = _relation_has_column(conn, relation, "opening_balance")
    has_opening_balance_type = _relation_has_column(conn, relation, "opening_balance_type")
    has_reminder_date = _relation_has_column(conn, relation, "reminder_date")
    opening_balance_type = str(payload.get("opening_balance_type") or "receivable").strip().lower()
    if opening_balance_type not in {"receivable", "payable"}:
        opening_balance_type = "receivable"
    set_parts = ["name = %(name)s", "updated_at = now()"]
    bind: dict[str, object] = {
        "customer_id": customer_id,
        "user_id": auth.user_id,
        "profile_id": profile_id,
        "name": name,
    }
    if has_phone:
        set_parts.append("phone = %(phone)s")
        bind["phone"] = (str(payload.get("phone")).strip() if payload.get("phone") else None)
    if has_address:
        set_parts.append("address = %(address)s")
        bind["address"] = (str(payload.get("address")).strip() if payload.get("address") else None)
    if has_category_id:
        set_parts.append("category_id = %(category_id)s")
        bind["category_id"] = payload.get("category_id")
    if has_opening_balance:
        opening_balance = float(payload.get("opening_balance") or 0)
        if opening_balance < 0:
            raise ApiError(status_code=400, code="invalid_opening_balance", message="opening_balance must be zero or greater.")
        set_parts.append("opening_balance = %(opening_balance)s")
        bind["opening_balance"] = opening_balance
    if has_opening_balance_type:
        set_parts.append("opening_balance_type = %(opening_balance_type)s")
        bind["opening_balance_type"] = opening_balance_type
    if has_reminder_date:
        set_parts.append("reminder_date = %(reminder_date)s")
        bind["reminder_date"] = payload.get("reminder_date")
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                update {relation}
                   set {", ".join(set_parts)}
                 where id = %(customer_id)s
                   and user_id = %(user_id)s
                   and profile_id = %(profile_id)s
                   and is_active = true
                returning *
                """,
                bind,
            )
            item = cur.fetchone()
    except UniqueViolation as exc:
        raise ApiError(
            status_code=409,
            code="customer_name_exists",
            message="Customer with this name already exists in this profile.",
        ) from exc
    except PsycopgError as exc:
        message = str(exc).strip()
        if "address" in message.lower() and "column" in message.lower():
            raise ApiError(
                status_code=500,
                code="customers_schema_mismatch",
                message="Customers address column is missing. Run latest migrations.",
            ) from exc
        raise ApiError(
            status_code=400,
            code="business_customer_update_failed",
            message=message or "Failed to update customer.",
        ) from exc
    if not item:
        raise ApiError(status_code=404, code="customer_not_found", message="Customer not found.")
    _enqueue_business_ai_refresh(conn, user_id=auth.user_id, profile_id=profile_id)
    return {"item": item}


@router.delete("/customers/{customer_id}")
def deactivate_business_customer(
    customer_id: str,
    profile_id: str = Query(...),
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    apply_db_auth_context(conn, auth.user_id)
    relation = _business_customers_relation(conn)
    if not relation:
        raise ApiError(status_code=500, code="customers_table_missing", message="Customer table is not available.")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            update {relation}
               set is_active = false,
                   updated_at = now()
             where id = %(customer_id)s
               and user_id = %(user_id)s
               and profile_id = %(profile_id)s
            returning id
            """,
            {"customer_id": customer_id, "user_id": auth.user_id, "profile_id": profile_id},
        )
        row = cur.fetchone()
    if not row:
        raise ApiError(status_code=404, code="customer_not_found", message="Customer not found.")
    _enqueue_business_ai_refresh(conn, user_id=auth.user_id, profile_id=profile_id)
    return {"ok": True}


@router.post("/suppliers")
def create_business_supplier(
    payload: dict,
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    profile_id = str(payload.get("profile_id") or "").strip()
    if not profile_id:
        raise ApiError(
            status_code=400,
            code="profile_required",
            message="profile_id is required.",
        )
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ApiError(
            status_code=400,
            code="name_required",
            message="name is required.",
        )

    opening_balance = round(float(payload.get("opening_balance") or 0), 2)
    if opening_balance < 0:
        raise ApiError(
            status_code=400,
            code="invalid_opening_balance",
            message="opening_balance must be zero or greater.",
        )
    opening_balance_type = str(payload.get("opening_balance_type") or "credit").strip().lower()
    if opening_balance_type not in {"credit", "advance"}:
        opening_balance_type = "credit"

    apply_db_auth_context(conn, auth.user_id)
    existing_receipt = _load_mutation_receipt(
        conn,
        user_id=auth.user_id,
        profile_id=profile_id,
        mutation_type="business.supplier-create",
        idempotency_key=payload.get("idempotency_key"),
    )
    if existing_receipt:
        return existing_receipt

    normalized_phone = str(payload.get("phone") or "").strip() or None
    normalized_address = str(payload.get("address") or "").strip() or None
    normalized_category_id = str(payload.get("category_id") or "").strip() or None
    normalized_reminder_date = str(payload.get("reminder_date") or "").strip() or None

    item: dict | None = None
    try:
        data = _execute_named_rpc(
            conn,
            "create_business_supplier_with_opening",
            {
                "p_profile_id": profile_id,
                "p_name": name,
                "p_phone": normalized_phone,
                "p_address": normalized_address,
                "p_category_id": normalized_category_id,
                "p_opening_balance": opening_balance,
                "p_opening_balance_type": opening_balance_type,
                "p_reminder_date": normalized_reminder_date,
            },
        )
        item = data if isinstance(data, dict) else None
        supplier_id = str(item.get("id") or "").strip() if item else ""

        if isinstance(data, str) and not supplier_id:
            supplier_id = data.strip()
        elif isinstance(data, list) and data:
            first = data[0]
            if isinstance(first, dict):
                item = first
                supplier_id = str(first.get("id") or "").strip()
            elif isinstance(first, str):
                supplier_id = first.strip()

        if supplier_id:
            refreshed = _load_business_supplier_item(
                conn,
                auth_user_id=auth.user_id,
                profile_id=profile_id,
                supplier_id=supplier_id,
            )
            if refreshed:
                item = refreshed
    except ApiError as exc:
        if exc.code not in {"business_rpc_missing", "business_rpc_failed"}:
            raise

    if not item:
        item = _create_business_supplier_direct(
            conn,
            auth_user_id=auth.user_id,
            profile_id=profile_id,
            name=name,
            phone=normalized_phone,
            address=normalized_address,
            category_id=normalized_category_id,
            opening_balance=opening_balance,
            opening_balance_type=opening_balance_type,
            reminder_date=normalized_reminder_date,
        )

    if not item:
        raise ApiError(
            status_code=500,
            code="supplier_create_unresolved",
            message="Supplier was created but could not be loaded. Please refresh and try again.",
        )

    _enqueue_business_ai_refresh(conn, user_id=auth.user_id, profile_id=profile_id)
    response = {"item": _to_json_safe(item)}
    _store_mutation_receipt(
        conn,
        user_id=auth.user_id,
        profile_id=profile_id,
        mutation_type="business.supplier-create",
        idempotency_key=payload.get("idempotency_key"),
        response_payload=jsonable_encoder(response),
    )
    return response


@router.patch("/suppliers/{supplier_id}")
def update_business_supplier(
    supplier_id: str,
    payload: dict,
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    profile_id = str(payload.get("profile_id") or "").strip()
    if not profile_id:
        raise ApiError(
            status_code=400,
            code="profile_required",
            message="profile_id is required.",
        )
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ApiError(
            status_code=400,
            code="name_required",
            message="name is required.",
        )

    apply_db_auth_context(conn, auth.user_id)
    data = _execute_named_rpc(
        conn,
        "update_business_supplier",
        {
            "p_profile_id": profile_id,
            "p_supplier_id": supplier_id,
            "p_name": name,
            "p_phone": str(payload.get("phone") or "").strip() or None,
            "p_address": str(payload.get("address") or "").strip() or None,
            "p_category_id": payload.get("category_id"),
            "p_reminder_date": payload.get("reminder_date"),
        },
    )
    item = data if isinstance(data, dict) else None
    if not item:
        item = _load_business_supplier_item(
            conn,
            auth_user_id=auth.user_id,
            profile_id=profile_id,
            supplier_id=supplier_id,
        )
    if not item:
        raise ApiError(
            status_code=404,
            code="supplier_not_found",
            message="Supplier not found.",
        )

    _enqueue_business_ai_refresh(conn, user_id=auth.user_id, profile_id=profile_id)
    return {"item": _to_json_safe(item)}


@router.delete("/suppliers/{supplier_id}")
def deactivate_business_supplier(
    supplier_id: str,
    profile_id: str = Query(...),
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    apply_db_auth_context(conn, auth.user_id)
    existing = _load_business_supplier_item(
        conn,
        auth_user_id=auth.user_id,
        profile_id=profile_id,
        supplier_id=supplier_id,
    )
    if not existing:
        raise ApiError(
            status_code=404,
            code="supplier_not_found",
            message="Supplier not found.",
        )

    _execute_named_rpc(
        conn,
        "deactivate_business_supplier",
        {
            "p_profile_id": profile_id,
            "p_supplier_id": supplier_id,
        },
    )
    _enqueue_business_ai_refresh(conn, user_id=auth.user_id, profile_id=profile_id)
    return {"ok": True}


@router.get("/customers/with-stats")
def list_business_customers_with_stats(
    profile_id: str = Query(...),
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    apply_db_auth_context(conn, auth.user_id)
    relation = _business_customers_relation(conn)
    if not relation:
        return {"items": []}
    invoices_relation = _business_invoices_relation(conn)
    invoice_payments_relation = _business_invoice_payments_relation(conn)
    ledger_postings_relation = _business_ledger_postings_relation(conn)
    ledger_due_by_customer: dict[str, float] = {}
    with conn.cursor() as cur:
        if not invoices_relation:
            cur.execute(
                f"""
                select
                  c.*,
                  0::numeric as total_bought,
                  0::numeric as total_paid,
                  0::bigint as invoice_count
                from {relation} c
                where c.user_id = %(user_id)s
                  and c.profile_id = %(profile_id)s
                  and c.is_active = true
                order by c.created_at asc
                """,
                {"user_id": auth.user_id, "profile_id": profile_id},
            )
            items = cur.fetchall() or []
        else:
            payments_sql = (
                f"""
                left join lateral (
                  select coalesce(sum(ip.amount), 0) as paid
                  from {invoice_payments_relation} ip
                  where ip.invoice_id = i.id
                ) p on true
                """
                if invoice_payments_relation
                else "left join lateral (select 0::numeric as paid) p on true"
            )
            cur.execute(
                f"""
                select
                  c.*,
                  coalesce(sum(i.total), 0) as total_bought,
                  coalesce(sum(p.paid), 0) as total_paid,
                  coalesce(count(distinct i.id), 0) as invoice_count
                from {relation} c
                left join {invoices_relation} i
                  on i.customer_id = c.id
                 and i.user_id = c.user_id
                 and i.profile_id = c.profile_id
                {payments_sql}
                where c.user_id = %(user_id)s
                  and c.profile_id = %(profile_id)s
                  and c.is_active = true
                group by c.id
                order by c.created_at asc
                """,
                {"user_id": auth.user_id, "profile_id": profile_id},
            )
            items = cur.fetchall() or []
        if ledger_postings_relation:
            cur.execute(
                f"""
                select
                  lp.ref_id::text as customer_id,
                  coalesce(
                    sum(
                      case
                        when lp.direction = 'debit' then lp.amount
                        when lp.direction = 'credit' then -lp.amount
                        else 0
                      end
                    ),
                    0
                  ) as due_amount
                from {ledger_postings_relation} lp
                where lp.user_id = %(user_id)s::uuid
                  and lp.profile_id = %(profile_id)s::uuid
                  and lp.leg_type = 'receivable'
                  and lp.ref_id is not null
                group by lp.ref_id
                """,
                {"user_id": auth.user_id, "profile_id": profile_id},
            )
            due_rows = cur.fetchall() or []
            for row in due_rows:
                customer_id = str(row.get("customer_id") or "").strip()
                if not customer_id:
                    continue
                due_amount = max(0.0, round(float(row.get("due_amount") or 0), 2))
                ledger_due_by_customer[customer_id] = due_amount
    for item in items:
        total_bought = float(item.get("total_bought") or 0)
        total_paid = float(item.get("total_paid") or 0)
        opening_balance = float(item.get("opening_balance") or 0)
        opening_type = str(item.get("opening_balance_type") or "receivable").lower()
        opening_effect = opening_balance if opening_type == "receivable" else -opening_balance
        fallback_due = total_bought - total_paid + opening_effect
        customer_id = str(item.get("id") or "").strip()
        ledger_due = ledger_due_by_customer.get(customer_id)
        resolved_due = ledger_due if ledger_due is not None else fallback_due
        item["total_bought"] = round(total_bought, 2)
        item["total_paid"] = round(total_paid, 2)
        item["total_due"] = round(max(0.0, resolved_due), 2)
        item["has_due"] = item["total_due"] > 0
    return {"items": items}


@router.get("/customers/{customer_id}/history", response_model=BusinessCustomerHistoryResponse)
def get_business_customer_history(
    customer_id: str,
    profile_id: str = Query(...),
    invoice_limit: int = Query(default=30, ge=1, le=100),
    payment_limit: int = Query(default=30, ge=1, le=100),
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    apply_db_auth_context(conn, auth.user_id)
    return fetch_business_customer_history(
        conn,
        user_id=auth.user_id,
        profile_id=profile_id,
        customer_id=customer_id,
        invoice_limit=invoice_limit,
        payment_limit=payment_limit,
    )


@router.get("/customers/{customer_id}/due")
def get_business_customer_due(
    customer_id: str,
    profile_id: str = Query(...),
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    apply_db_auth_context(conn, auth.user_id)
    ledger_postings_relation = _business_ledger_postings_relation(conn)
    if not ledger_postings_relation:
        return {"due": 0.0}
    with conn.cursor() as cur:
        cur.execute(
            f"""
            select direction, amount
            from {ledger_postings_relation}
            where user_id = %(user_id)s
              and profile_id = %(profile_id)s
              and leg_type = 'receivable'
              and ref_id = %(customer_id)s
            """,
            {"user_id": auth.user_id, "profile_id": profile_id, "customer_id": customer_id},
        )
        rows = cur.fetchall() or []
    due = 0.0
    for row in rows:
        amount = float(row.get("amount") or 0)
        due += amount if row.get("direction") == "debit" else -amount
    return {"due": round(max(0.0, due), 2)}


@router.get("/inventory/movements")
def list_business_inventory_movements(
    profile_id: str = Query(...),
    limit: int = Query(default=250, ge=1, le=2000),
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    apply_db_auth_context(conn, auth.user_id)
    relation = _business_inventory_movements_relation(conn)
    if not relation:
        return {"items": []}
    with conn.cursor() as cur:
        cur.execute(
            f"""
            select *
            from {relation}
            where user_id = %(user_id)s
              and profile_id = %(profile_id)s
              and ref_type <> 'opening'
            order by date desc, created_at desc
            limit %(limit)s
            """,
            {"user_id": auth.user_id, "profile_id": profile_id, "limit": limit},
        )
        items = cur.fetchall() or []
    return {"items": items}


def _load_business_product_list_item(
    conn: Connection,
    *,
    auth_user_id: str,
    profile_id: str,
    product_id: str,
) -> dict | None:
    try:
        with conn.cursor() as cur:
            cur.execute(
                "select public.assert_active_business_profile(%(user_id)s::uuid, %(profile_id)s::uuid)",
                {"user_id": auth_user_id, "profile_id": profile_id},
            )
            cur.execute(
                """
                select
                  id::text as id,
                  user_id::text as user_id,
                  profile_id::text as profile_id,
                  name,
                  price,
                  price as selling_price,
                  quantity,
                  unit_id::text as unit_id,
                  category_id::text as category_id,
                  sku,
                  is_active,
                  created_at::text as created_at,
                  updated_at::text as updated_at,
                  coalesce(unit_name, '') as unit_name,
                  coalesce(category_name, '') as category_name
                from public.list_business_products(%(profile_id)s::uuid)
                where id = %(product_id)s::uuid
                  and user_id = %(user_id)s::uuid
                limit 1
                """,
                {
                    "user_id": auth_user_id,
                    "profile_id": profile_id,
                    "product_id": product_id,
                },
            )
            item = cur.fetchone() or None
            if item:
                return item
    except (UndefinedFunction, UndefinedTable):
        pass

    relation = _business_products_relation(conn)
    if not relation:
        return None

    has_profile_id = _relation_has_column(conn, relation, "profile_id")
    has_price = _relation_has_column(conn, relation, "price")
    has_selling_price = _relation_has_column(conn, relation, "selling_price")
    has_quantity = _relation_has_column(conn, relation, "quantity")
    has_unit_id = _relation_has_column(conn, relation, "unit_id")
    has_category_id = _relation_has_column(conn, relation, "category_id")
    has_sku = _relation_has_column(conn, relation, "sku")
    has_is_active = _relation_has_column(conn, relation, "is_active")
    has_created_at = _relation_has_column(conn, relation, "created_at")
    has_updated_at = _relation_has_column(conn, relation, "updated_at")

    with conn.cursor() as cur:
        cur.execute(
            f"""
            select
              p.id::text as id,
              p.user_id::text as user_id,
              {( "p.profile_id::text" if has_profile_id else "%(profile_id)s::text" )} as profile_id,
              p.name,
              {( "p.price::numeric" if has_price else "0::numeric" )} as price,
              {( "p.selling_price::numeric" if has_selling_price else ("p.price::numeric" if has_price else "0::numeric") )} as selling_price,
              {( "p.quantity::numeric" if has_quantity else "0::numeric" )} as quantity,
              {( "p.unit_id::text" if has_unit_id else "''::text" )} as unit_id,
              {( "p.category_id::text" if has_category_id else "null::text" )} as category_id,
              {( "p.sku" if has_sku else "null::text" )} as sku,
              {( "p.is_active" if has_is_active else "true" )} as is_active,
              {( "p.created_at::text" if has_created_at else "now()::text" )} as created_at,
              {( "p.updated_at::text" if has_updated_at else "now()::text" )} as updated_at,
              ''::text as unit_name,
              ''::text as category_name
            from {relation} p
            where p.id = %(product_id)s::uuid
              and p.user_id = %(user_id)s::uuid
              {( "and p.profile_id = %(profile_id)s::uuid" if has_profile_id else "" )}
            limit 1
            """,
            {
                "product_id": product_id,
                "user_id": auth_user_id,
                "profile_id": profile_id,
            },
        )
        return cur.fetchone() or None


def _load_business_product_list_item_direct(
    conn: Connection,
    *,
    auth_user_id: str,
    profile_id: str,
    product_id: str,
) -> dict | None:
    relation = _business_products_relation(conn)
    if not relation:
        return None

    has_profile_id = _relation_has_column(conn, relation, "profile_id")
    has_price = _relation_has_column(conn, relation, "price")
    has_selling_price = _relation_has_column(conn, relation, "selling_price")
    has_quantity = _relation_has_column(conn, relation, "quantity")
    has_unit_id = _relation_has_column(conn, relation, "unit_id")
    has_category_id = _relation_has_column(conn, relation, "category_id")
    has_sku = _relation_has_column(conn, relation, "sku")
    has_is_active = _relation_has_column(conn, relation, "is_active")
    has_created_at = _relation_has_column(conn, relation, "created_at")
    has_updated_at = _relation_has_column(conn, relation, "updated_at")

    units_relation = _first_existing_relation(conn, ["business.units", "public.units"]) if has_unit_id else None
    has_units_profile_id = (
        _relation_has_column(conn, units_relation, "profile_id") if units_relation else False
    )
    has_units_active = (
        _relation_has_column(conn, units_relation, "is_active") if units_relation else False
    )

    category_relation = (
        _legacy_category_relation_for_domain(conn, "product") if has_category_id else None
    )
    has_category_profile_id = (
        _relation_has_column(conn, category_relation, "profile_id") if category_relation else False
    )
    has_category_active = (
        _relation_has_column(conn, category_relation, "is_active") if category_relation else False
    )
    use_unified_categories = (
        has_category_id and not category_relation and _has_unified_business_categories(conn)
    )

    units_join_sql = ""
    unit_name_sql = "''::text as unit_name"
    if units_relation and has_unit_id:
        units_join_sql = f"""
            left join {units_relation} u
              on u.id = p.unit_id
             and u.user_id = %(user_id)s::uuid
             {"and u.profile_id = %(profile_id)s::uuid" if has_units_profile_id else ""}
             {"and u.is_active = true" if has_units_active else ""}
        """
        unit_name_sql = "coalesce(u.name, '')::text as unit_name"

    category_join_sql = ""
    category_name_sql = "''::text as category_name"
    if category_relation and has_category_id:
        category_join_sql = f"""
            left join {category_relation} c
              on c.id = p.category_id
             and c.user_id = %(user_id)s::uuid
             {"and c.profile_id = %(profile_id)s::uuid" if has_category_profile_id else ""}
             {"and c.is_active = true" if has_category_active else ""}
        """
        category_name_sql = "coalesce(c.name, '')::text as category_name"
    elif use_unified_categories and has_category_id:
        category_join_sql = """
            left join public.business_categories c
              on c.id = p.category_id
             and c.user_id = %(user_id)s::uuid
             and c.profile_id = %(profile_id)s::uuid
             and c.domain = 'product'
             and c.is_active = true
        """
        category_name_sql = "coalesce(c.name, '')::text as category_name"

    with conn.cursor() as cur:
        cur.execute(
            f"""
            select
              p.id::text as id,
              p.user_id::text as user_id,
              {( "p.profile_id::text" if has_profile_id else "%(profile_id)s::text" )} as profile_id,
              p.name,
              {( "p.price::numeric" if has_price else "0::numeric" )} as price,
              {( "p.selling_price::numeric" if has_selling_price else ("p.price::numeric" if has_price else "0::numeric") )} as selling_price,
              {( "p.quantity::numeric" if has_quantity else "0::numeric" )} as quantity,
              {( "p.unit_id::text" if has_unit_id else "''::text" )} as unit_id,
              {( "p.category_id::text" if has_category_id else "null::text" )} as category_id,
              {( "p.sku" if has_sku else "null::text" )} as sku,
              {( "p.is_active" if has_is_active else "true" )} as is_active,
              {( "p.created_at::text" if has_created_at else "now()::text" )} as created_at,
              {( "p.updated_at::text" if has_updated_at else "now()::text" )} as updated_at,
              {unit_name_sql},
              {category_name_sql}
            from {relation} p
            {units_join_sql}
            {category_join_sql}
            where p.id = %(product_id)s::uuid
              and p.user_id = %(user_id)s::uuid
              {( "and p.profile_id = %(profile_id)s::uuid" if has_profile_id else "" )}
            limit 1
            """,
            {
                "product_id": product_id,
                "user_id": auth_user_id,
                "profile_id": profile_id,
            },
        )
        return cur.fetchone() or None


def _find_business_product_id_by_identity(
    conn: Connection,
    *,
    auth_user_id: str,
    profile_id: str,
    name: str,
    unit_id: str,
    category_id: str | None,
    sku: str | None,
) -> str | None:
    relation = _business_products_relation(conn)
    if not relation:
        return None

    has_profile_id = _relation_has_column(conn, relation, "profile_id")
    has_unit_id = _relation_has_column(conn, relation, "unit_id")
    has_category_id = _relation_has_column(conn, relation, "category_id")
    has_sku = _relation_has_column(conn, relation, "sku")
    has_is_active = _relation_has_column(conn, relation, "is_active")
    has_created_at = _relation_has_column(conn, relation, "created_at")

    base_where = ["user_id = %(user_id)s::uuid"]
    if has_profile_id:
        base_where.append("profile_id = %(profile_id)s::uuid")
    if has_is_active:
        base_where.append("is_active = true")

    order_sql = "created_at desc" if has_created_at else "id::text desc"
    with conn.cursor() as cur:
        if has_sku and str(sku or "").strip():
            cur.execute(
                f"""
                select id::text as id
                from {relation}
                where {" and ".join(base_where)}
                  and lower(coalesce(sku, '')) = lower(%(sku)s::text)
                order by {order_sql}
                limit 1
                """,
                {
                    "user_id": auth_user_id,
                    "profile_id": profile_id,
                    "sku": str(sku or "").strip(),
                },
            )
            row = cur.fetchone() or {}
            resolved = str(row.get("id") or "").strip()
            if resolved:
                return resolved

        where_name = [*base_where, "lower(trim(name)) = lower(trim(%(name)s::text))"]
        if has_unit_id:
            where_name.append("unit_id = %(unit_id)s::uuid")
        if has_category_id:
            if str(category_id or "").strip():
                where_name.append("category_id = %(category_id)s::uuid")
            else:
                where_name.append("category_id is null")
        cur.execute(
            f"""
            select id::text as id
            from {relation}
            where {" and ".join(where_name)}
            order by {order_sql}
            limit 1
            """,
            {
                "user_id": auth_user_id,
                "profile_id": profile_id,
                "name": name,
                "unit_id": unit_id,
                "category_id": category_id,
            },
        )
        row = cur.fetchone() or {}
        resolved = str(row.get("id") or "").strip()
        return resolved or None


def _create_business_product_direct(
    conn: Connection,
    *,
    auth_user_id: str,
    profile_id: str,
    name: str,
    unit_id: str,
    category_id: str | None,
    sku: str | None,
    price: float,
    selling_price: float,
    quantity: float,
) -> str:
    relation = _business_products_relation(conn)
    if not relation:
        raise ApiError(
            status_code=500,
            code="products_table_missing",
            message="Product table is not available.",
        )

    normalized_name = str(name or "").strip()
    normalized_unit_id = str(unit_id or "").strip()
    normalized_category_id = str(category_id or "").strip() or None
    normalized_sku = str(sku or "").strip() or None
    if not normalized_name:
        raise ApiError(status_code=400, code="name_required", message="name is required.")
    if not normalized_unit_id:
        raise ApiError(status_code=400, code="unit_required", message="unit_id is required.")

    rounded_price = round(max(0.0, float(price or 0)), 2)
    rounded_selling_price = round(max(0.0, float(selling_price or 0)), 2)
    rounded_quantity = round(max(0.0, float(quantity or 0)), 3)

    has_assert_active_profile = _function_exists(
        conn,
        "public.assert_active_business_profile(uuid,uuid)",
    )
    has_assert_profile_owner = _function_exists(
        conn,
        "public.assert_profile_ownership(uuid,uuid)",
    )

    has_profile_id = _relation_has_column(conn, relation, "profile_id")
    has_unit_id = _relation_has_column(conn, relation, "unit_id")
    has_category_id = _relation_has_column(conn, relation, "category_id")
    has_sku = _relation_has_column(conn, relation, "sku")
    has_price = _relation_has_column(conn, relation, "price")
    has_selling_price = _relation_has_column(conn, relation, "selling_price")
    has_quantity = _relation_has_column(conn, relation, "quantity")
    has_is_active = _relation_has_column(conn, relation, "is_active")
    has_updated_at = _relation_has_column(conn, relation, "updated_at")

    units_relation = _first_existing_relation(conn, ["business.units", "public.units"])
    if units_relation:
        has_units_profile_id = _relation_has_column(conn, units_relation, "profile_id")
        has_units_active = _relation_has_column(conn, units_relation, "is_active")
        with conn.cursor() as cur:
            unit_where = ["id = %(unit_id)s::uuid", "user_id = %(user_id)s::uuid"]
            if has_units_profile_id:
                unit_where.append("profile_id = %(profile_id)s::uuid")
            if has_units_active:
                unit_where.append("is_active = true")
            cur.execute(
                f"""
                select id::text as id
                from {units_relation}
                where {" and ".join(unit_where)}
                limit 1
                """,
                {
                    "unit_id": normalized_unit_id,
                    "user_id": auth_user_id,
                    "profile_id": profile_id,
                },
            )
            if not cur.fetchone():
                raise ApiError(
                    status_code=400,
                    code="invalid_unit",
                    message="Selected unit is invalid for this profile.",
                )

    if normalized_category_id and has_category_id:
        category_valid = False
        legacy_product_category_relation = _legacy_category_relation_for_domain(conn, "product")
        if legacy_product_category_relation:
            has_cat_profile_id = _relation_has_column(conn, legacy_product_category_relation, "profile_id")
            has_cat_active = _relation_has_column(conn, legacy_product_category_relation, "is_active")
            with conn.cursor() as cur:
                where_parts = [
                    "id = %(category_id)s::uuid",
                    "user_id = %(user_id)s::uuid",
                ]
                if has_cat_profile_id:
                    where_parts.append("profile_id = %(profile_id)s::uuid")
                if has_cat_active:
                    where_parts.append("is_active = true")
                cur.execute(
                    f"""
                    select id::text as id
                    from {legacy_product_category_relation}
                    where {" and ".join(where_parts)}
                    limit 1
                    """,
                    {
                        "category_id": normalized_category_id,
                        "user_id": auth_user_id,
                        "profile_id": profile_id,
                    },
                )
                category_valid = bool(cur.fetchone())

        if not category_valid and _has_unified_business_categories(conn):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select id::text as id
                    from public.business_categories
                    where id = %(category_id)s::uuid
                      and user_id = %(user_id)s::uuid
                      and profile_id = %(profile_id)s::uuid
                      and domain = 'product'
                      and is_active = true
                    limit 1
                    """,
                    {
                        "category_id": normalized_category_id,
                        "user_id": auth_user_id,
                        "profile_id": profile_id,
                    },
                )
                category_valid = bool(cur.fetchone())

        if not category_valid:
            raise ApiError(
                status_code=400,
                code="invalid_category",
                message="Selected category is invalid for this profile.",
            )

    if has_assert_active_profile:
        with conn.cursor() as cur:
            cur.execute(
                "select public.assert_active_business_profile(%(user_id)s::uuid, %(profile_id)s::uuid)",
                {"user_id": auth_user_id, "profile_id": profile_id},
            )
    elif has_assert_profile_owner:
        with conn.cursor() as cur:
            cur.execute(
                "select public.assert_profile_ownership(%(user_id)s::uuid, %(profile_id)s::uuid)",
                {"user_id": auth_user_id, "profile_id": profile_id},
            )

    try:
        # Savepoint scope keeps the parent transaction usable when insert hits a unique conflict.
        with conn.transaction():
            with conn.cursor() as cur:
                insert_fields = ["user_id", "name"]
                insert_values = ["%(user_id)s::uuid", "%(name)s::text"]
                bind: dict[str, object] = {
                    "user_id": auth_user_id,
                    "profile_id": profile_id,
                    "name": normalized_name,
                    "unit_id": normalized_unit_id,
                    "category_id": normalized_category_id,
                    "sku": normalized_sku,
                    "price": rounded_price,
                    "selling_price": rounded_selling_price,
                    "quantity": rounded_quantity,
                }

                if has_profile_id:
                    insert_fields.append("profile_id")
                    insert_values.append("%(profile_id)s::uuid")
                if has_price:
                    insert_fields.append("price")
                    insert_values.append("%(price)s::numeric")
                if has_selling_price:
                    insert_fields.append("selling_price")
                    insert_values.append("%(selling_price)s::numeric")
                if has_quantity:
                    insert_fields.append("quantity")
                    insert_values.append("%(quantity)s::numeric")
                if has_unit_id:
                    insert_fields.append("unit_id")
                    insert_values.append("%(unit_id)s::uuid")
                if has_category_id:
                    insert_fields.append("category_id")
                    insert_values.append("%(category_id)s::uuid")
                if has_sku:
                    insert_fields.append("sku")
                    insert_values.append("%(sku)s::text")
                if has_is_active:
                    insert_fields.append("is_active")
                    insert_values.append("true")

                cur.execute(
                    f"""
                    insert into {relation} ({", ".join(insert_fields)})
                    values ({", ".join(insert_values)})
                    returning id::text as id
                    """,
                    bind,
                )
                row = cur.fetchone() or {}
                created_product_id = str(row.get("id") or "").strip()
                if created_product_id:
                    return created_product_id
    except UniqueViolation as exc:
        existing_product_id = _find_business_product_id_by_identity(
            conn,
            auth_user_id=auth_user_id,
            profile_id=profile_id,
            name=normalized_name,
            unit_id=normalized_unit_id,
            category_id=normalized_category_id,
            sku=normalized_sku,
        )
        if existing_product_id:
            return existing_product_id

        constraint_name = ""
        diag = getattr(exc, "diag", None)
        if diag is not None:
            constraint_name = str(getattr(diag, "constraint_name", "") or "")
        if constraint_name in _PRODUCT_DUPLICATE_NAME_CONSTRAINTS:
            raise ApiError(
                status_code=409,
                code="product_name_exists",
                message="Product name already exists for this business profile. Please use a unique name.",
            ) from exc
        if constraint_name in _PRODUCT_DUPLICATE_SKU_CONSTRAINTS:
            raise ApiError(
                status_code=409,
                code="product_sku_exists",
                message="SKU already exists for this business profile. Please use a unique SKU.",
            ) from exc
        raise ApiError(
            status_code=409,
            code="duplicate_record",
            message="A product with the same unique value already exists.",
        ) from exc
    except PsycopgError as exc:
        raise ApiError(
            status_code=400,
            code="business_product_create_failed",
            message=str(exc).strip() or "Failed to create product.",
        ) from exc

    fallback_id = _find_business_product_id_by_identity(
        conn,
        auth_user_id=auth_user_id,
        profile_id=profile_id,
        name=normalized_name,
        unit_id=normalized_unit_id,
        category_id=normalized_category_id,
        sku=normalized_sku,
    )
    if fallback_id:
        return fallback_id

    raise ApiError(
        status_code=500,
        code="product_create_unresolved",
        message="Product created but product id could not be resolved. Please refresh and try again.",
    )


@router.post("/products", response_model=BusinessProductCreateResponse)
def create_business_product_endpoint(
    payload: BusinessProductCreateRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> BusinessProductCreateResponse:
    apply_db_auth_context(conn, auth.user_id)
    endpoint_started_at = perf_counter()

    existing_receipt = _load_mutation_receipt(
        conn,
        user_id=auth.user_id,
        profile_id=payload.profile_id,
        mutation_type="business.product-create",
        idempotency_key=payload.idempotency_key,
    )
    if existing_receipt:
        return BusinessProductCreateResponse(**existing_receipt)

    opening_qty = round(max(0.0, float(payload.opening_qty or 0)), 3)
    opening_unit_cost = round(max(0.0, float(payload.opening_unit_cost or 0)), 2)
    if opening_qty > 0 and opening_unit_cost <= 0:
        raise ApiError(
            status_code=400,
            code="opening_unit_cost_required",
            message="Opening unit cost must be greater than zero when opening inventory is set.",
        )

    create_step_ms = 0.0
    opening_step_ms = 0.0
    load_step_ms = 0.0
    receipt_step_ms = 0.0
    used_direct_load = False
    used_fallback_load = False

    with conn.transaction():
        created_product_id = ""
        create_step_started_at = perf_counter()
        try:
            created = _execute_named_rpc(
                conn,
                "create_business_product",
                {
                    "p_profile_id": payload.profile_id,
                    "p_name": payload.name.strip(),
                    "p_price": payload.price,
                    "p_selling_price": payload.selling_price,
                    "p_quantity": payload.quantity,
                    "p_unit_id": payload.unit_id,
                    "p_category_id": payload.category_id,
                    "p_sku": payload.sku,
                },
            )
            created_row = created if isinstance(created, dict) else {}
            created_product_id = str(created_row.get("id") or "").strip()
        except ApiError as exc:
            if exc.code != "business_rpc_missing":
                raise
            created_product_id = _create_business_product_direct(
                conn,
                auth_user_id=auth.user_id,
                profile_id=payload.profile_id,
                name=payload.name.strip(),
                unit_id=payload.unit_id,
                category_id=payload.category_id,
                sku=payload.sku,
                price=payload.price,
                selling_price=payload.selling_price,
                quantity=payload.quantity,
            )
        create_step_ms = (perf_counter() - create_step_started_at) * 1000

        if not created_product_id:
            created_product_id = (
                _find_business_product_id_by_identity(
                    conn,
                    auth_user_id=auth.user_id,
                    profile_id=payload.profile_id,
                    name=payload.name.strip(),
                    unit_id=payload.unit_id,
                    category_id=payload.category_id,
                    sku=payload.sku,
                )
                or ""
            )

        if not created_product_id:
            raise ApiError(
                status_code=500,
                code="product_create_unresolved",
                message="Product created but product id could not be resolved. Please refresh and try again.",
            )

        if opening_qty > 0:
            opening_step_started_at = perf_counter()
            try:
                _execute_named_rpc(
                    conn,
                    "create_business_opening_stock_entry",
                    {
                        "p_user_id": auth.user_id,
                        "p_profile_id": payload.profile_id,
                        "p_product_id": created_product_id,
                        "p_qty": opening_qty,
                        "p_unit_cost": opening_unit_cost,
                        "p_date": payload.opening_date or date.today().isoformat(),
                        "p_note": payload.opening_note or "Opening inventory from product creation",
                    },
                )
            except ApiError as exc:
                if exc.code != "business_rpc_missing":
                    raise
                _create_business_opening_stock_entry_direct(
                    conn,
                    user_id=auth.user_id,
                    profile_id=payload.profile_id,
                    product_id=created_product_id,
                    qty=opening_qty,
                    unit_cost=opening_unit_cost,
                    date_value=payload.opening_date or date.today().isoformat(),
                    note=payload.opening_note or "Opening inventory from product creation",
                )
            opening_step_ms = (perf_counter() - opening_step_started_at) * 1000

        load_step_started_at = perf_counter()
        product_row = _load_business_product_list_item_direct(
            conn,
            auth_user_id=auth.user_id,
            profile_id=payload.profile_id,
            product_id=created_product_id,
        )
        used_direct_load = product_row is not None
        if not product_row:
            used_fallback_load = True
            product_row = _load_business_product_list_item(
                conn,
                auth_user_id=auth.user_id,
                profile_id=payload.profile_id,
                product_id=created_product_id,
            )
        load_step_ms = (perf_counter() - load_step_started_at) * 1000
        if not product_row:
            raise ApiError(
                status_code=500,
                code="product_create_missing",
                message="Product was created but could not be loaded. Please refresh and try again.",
            )

    _enqueue_business_ai_refresh(
        conn,
        user_id=auth.user_id,
        profile_id=payload.profile_id,
    )

    response = BusinessProductCreateResponse(
        product=BusinessProductListItem(
            id=str(product_row.get("id") or ""),
            user_id=str(product_row.get("user_id") or ""),
            profile_id=str(product_row.get("profile_id") or ""),
            name=str(product_row.get("name") or ""),
            price=float(product_row.get("price") or 0),
            selling_price=float(product_row.get("selling_price") or product_row.get("price") or 0),
            quantity=float(product_row.get("quantity") or 0),
            unit_id=str(product_row.get("unit_id") or ""),
            category_id=str(product_row.get("category_id") or "") or None,
            sku=str(product_row.get("sku") or "") or None,
            is_active=bool(product_row.get("is_active")),
            created_at=str(product_row.get("created_at") or "") or None,
            updated_at=str(product_row.get("updated_at") or "") or None,
            unit_name=str(product_row.get("unit_name") or "") or None,
            category_name=str(product_row.get("category_name") or "") or None,
        ),
        opening_posted=opening_qty > 0,
        occurred_on=payload.opening_date or date.today().isoformat(),
    )
    receipt_step_started_at = perf_counter()
    _store_mutation_receipt(
        conn,
        user_id=auth.user_id,
        profile_id=payload.profile_id,
        mutation_type="business.product-create",
        idempotency_key=payload.idempotency_key,
        response_payload=response.model_dump(),
    )
    receipt_step_ms = (perf_counter() - receipt_step_started_at) * 1000
    total_step_ms = (perf_counter() - endpoint_started_at) * 1000
    print(
        "[Perf] api POST /business/products stages:"
        f" total={total_step_ms:.1f}ms"
        f" create={create_step_ms:.1f}ms"
        f" opening={opening_step_ms:.1f}ms"
        f" load={load_step_ms:.1f}ms"
        f" receipt={receipt_step_ms:.1f}ms"
        f" direct_load={1 if used_direct_load else 0}"
        f" fallback_load={1 if used_fallback_load else 0}"
    )
    return response


@router.get("/products/feed")
def list_business_products_feed(
    profile_id: str = Query(...),
    search: str | None = Query(default=None),
    include_inactive: bool = Query(default=False),
    limit: int = Query(default=40, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    apply_db_auth_context(conn, auth.user_id)

    bind: dict[str, object] = {
        "profile_id": profile_id,
        "search": f"%{search.strip()}%" if search and search.strip() else None,
        "include_inactive": include_inactive,
        "query_limit": limit + 1,
        "offset": offset,
    }

    where = ["(%(include_inactive)s or is_active = true)"]
    if bind["search"]:
        where.append(
            "(name ilike %(search)s or coalesce(sku, '') ilike %(search)s or coalesce(category_name, '') ilike %(search)s)"
        )

    category_counts: list[dict] = []
    with conn.cursor() as cur:
        if offset == 0:
            cur.execute(
                f"""
                select
                  category_id::text as category_id,
                  coalesce(category_name, '') as category_name,
                  count(*)::int as total_count
                from public.list_business_products(%(profile_id)s::uuid)
                where {' and '.join(where)}
                group by category_id, category_name
                order by case when coalesce(category_name, '') = '' then 1 else 0 end,
                         lower(coalesce(category_name, '')),
                         category_id::text
                """,
                bind,
            )
            category_counts = cur.fetchall() or []

        cur.execute(
            f"""
            select
              id::text as id,
              user_id::text as user_id,
              profile_id::text as profile_id,
              name,
              price,
              quantity,
              unit_id::text as unit_id,
              category_id::text as category_id,
              sku,
              is_active,
              created_at::text as created_at,
              updated_at::text as updated_at,
              coalesce(unit_name, '') as unit_name,
              coalesce(category_name, '') as category_name
            from public.list_business_products(%(profile_id)s::uuid)
            where {' and '.join(where)}
            order by case when coalesce(category_name, '') = '' then 1 else 0 end,
                     lower(coalesce(category_name, '')),
                     lower(name),
                     id::text
            limit %(query_limit)s
            offset %(offset)s
            """,
            bind,
        )
        rows = cur.fetchall() or []

    has_more = len(rows) > limit
    page_items = rows[:limit]
    next_offset = offset + len(page_items) if has_more else None
    return {
        "items": page_items,
        "has_more": has_more,
        "next_offset": next_offset,
        "category_counts": category_counts,
    }


@router.patch("/products/{product_id}/selling-price")
def patch_business_product_selling_price(
    product_id: UUID,
    payload: dict,
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    profile_id = str(payload.get("profile_id") or "").strip()
    if not profile_id:
        raise ApiError(status_code=400, code="profile_required", message="profile_id is required.")
    apply_db_auth_context(conn, auth.user_id)
    selling_price = round(max(0.0, float(payload.get("selling_price") or 0)), 2)
    updated = _sync_business_product_selling_price(
        conn,
        user_id=auth.user_id,
        profile_id=profile_id,
        product_id=str(product_id),
        selling_price=selling_price,
        allow_zero=True,
    )

    if not updated:
        item = _load_business_product_list_item(
            conn,
            auth_user_id=auth.user_id,
            profile_id=profile_id,
            product_id=str(product_id),
        )
        if not item:
            raise ApiError(status_code=404, code="product_not_found", message="Product not found.")
    _enqueue_business_ai_refresh(conn, user_id=auth.user_id, profile_id=profile_id)
    return {
        "item": {
            "id": str(product_id),
            "selling_price": selling_price,
        }
    }


@router.get("/products/selling-prices")
def list_business_product_selling_prices(
    profile_id: str = Query(...),
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    apply_db_auth_context(conn, auth.user_id)
    relation = _business_products_relation(conn)
    if not relation:
        return {"items": []}

    has_profile_id = _relation_has_column(conn, relation, "profile_id")
    has_price_col = _relation_has_column(conn, relation, "price")
    has_selling_price_col = _relation_has_column(conn, relation, "selling_price")
    has_is_active = _relation_has_column(conn, relation, "is_active")

    if has_selling_price_col and has_price_col:
        selling_price_sql = "coalesce(p.selling_price, p.price, 0)::double precision"
    elif has_selling_price_col:
        selling_price_sql = "coalesce(p.selling_price, 0)::double precision"
    elif has_price_col:
        selling_price_sql = "coalesce(p.price, 0)::double precision"
    else:
        selling_price_sql = "0::double precision"

    where_clauses = ["p.user_id = %(user_id)s"]
    if has_profile_id:
        where_clauses.append("p.profile_id = %(profile_id)s")
    if has_is_active:
        where_clauses.append("p.is_active = true")

    with conn.cursor() as cur:
        cur.execute(
            f"""
            select
              p.id::text as id,
              {selling_price_sql} as selling_price
            from {relation} p
            where {" and ".join(where_clauses)}
            """,
            {"user_id": auth.user_id, "profile_id": profile_id},
        )
        items = cur.fetchall() or []
    safe_items = _to_json_safe(items)
    return {"items": safe_items if isinstance(safe_items, list) else []}


@router.get("/invoices")
def list_business_invoices(
    profile_id: str = Query(...),
    status: str | None = Query(default=None),
    search: str | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    view: str | None = Query(default=None),
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    apply_db_auth_context(conn, auth.user_id)
    invoices_relation = _business_invoices_relation(conn)
    if not invoices_relation:
        return {"items": []}
    where = ["i.user_id = %(user_id)s", "i.profile_id = %(profile_id)s"]
    bind: dict[str, object] = {"user_id": auth.user_id, "profile_id": profile_id}
    if status and status != "all":
        where.append("i.payment_status = %(status)s")
        bind["status"] = status
    if date_from:
        where.append("i.date >= %(date_from)s")
        bind["date_from"] = date_from
    if date_to:
        where.append("i.date <= %(date_to)s")
        bind["date_to"] = date_to
    if search:
        where.append("(i.invoice_no ilike %(search)s or i.customer_name_snapshot ilike %(search)s)")
        bind["search"] = f"%{search.strip()}%"
    if limit:
        bind["query_limit"] = limit + 1
    if offset > 0:
        bind["offset"] = offset
    select_clause = "i.*"
    if view == "list":
        has_due_amount = _relation_has_column(conn, invoices_relation, "due_amount")
        has_paid_amount = _relation_has_column(conn, invoices_relation, "paid_amount")
        invoice_payments_relation = _business_invoice_payments_relation(conn)
        if has_paid_amount:
            paid_amount_expr = "coalesce(i.paid_amount, 0::numeric)"
        elif invoice_payments_relation:
            paid_amount_expr = (
                "coalesce((select sum(coalesce(ip.amount, 0))::numeric "
                f"from {invoice_payments_relation} ip "
                "where ip.invoice_id = i.id), 0::numeric)"
            )
        else:
            paid_amount_expr = "0::numeric"
        due_amount_expr = (
            f"coalesce(i.due_amount, greatest(coalesce(i.total, 0) - ({paid_amount_expr}), 0::numeric))"
            if has_due_amount
            else f"greatest(coalesce(i.total, 0) - ({paid_amount_expr}), 0::numeric)"
        )
        select_clause = """
            i.id,
            i.user_id,
            i.profile_id,
            i.invoice_no,
            i.customer_id,
            i.date,
            i.subtotal,
            i.discount,
            i.tax,
            i.total,
            i.payment_status,
            i.note,
            i.customer_name_snapshot,
            i.customer_phone_snapshot,
            """
        select_clause += f"""
            ({paid_amount_expr}) as paid_amount,
            ({due_amount_expr}) as due_amount,
            i.created_at,
            i.updated_at
        """
    limit_sql = "limit %(query_limit)s" if limit else ""
    offset_sql = "offset %(offset)s" if offset > 0 else ""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            select {select_clause}
            from {invoices_relation} i
            where {' and '.join(where)}
            order by i.date desc, i.created_at desc, i.id desc
            {limit_sql}
            {offset_sql}
            """,
            bind,
        )
        items = cur.fetchall() or []
    if not limit:
        return {"items": items}

    has_more = len(items) > limit
    page_items = items[:limit]
    next_offset = offset + len(page_items) if has_more else None
    return {
        "items": page_items,
        "has_more": has_more,
        "next_offset": next_offset,
    }


@router.post("/invoices/draft")
def create_business_invoice_draft(
    payload: dict,
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    profile_id = str(payload.get("profile_id") or "").strip()
    if not profile_id:
        raise ApiError(status_code=400, code="profile_required", message="profile_id is required.")
    apply_db_auth_context(conn, auth.user_id)
    invoices_relation = _business_invoices_relation(conn)
    if not invoices_relation:
        raise ApiError(status_code=500, code="invoices_table_missing", message="Invoice table is not available.")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            insert into {invoices_relation} (
              user_id, profile_id, invoice_no, customer_id, date, payment_status, note
            ) values (
              %(user_id)s, %(profile_id)s, %(invoice_no)s, %(customer_id)s, %(date)s, 'due', %(note)s
            )
            returning *
            """,
            {
                "user_id": auth.user_id,
                "profile_id": profile_id,
                "invoice_no": f"DRAFT-{int(__import__('time').time() * 1000)}",
                "customer_id": payload.get("customer_id"),
                "date": payload.get("date"),
                "note": payload.get("note"),
            },
        )
        item = cur.fetchone()
    if not item:
        raise ApiError(status_code=500, code="invoice_create_failed", message="Could not create invoice draft.")
    _enqueue_business_ai_refresh(conn, user_id=auth.user_id, profile_id=profile_id)
    return {"item": item}


@router.get("/invoices/{invoice_id}/detail")
def get_business_invoice_detail(
    invoice_id: str,
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    apply_db_auth_context(conn, auth.user_id)
    invoices_relation = _business_invoices_relation(conn)
    if not invoices_relation:
        raise ApiError(status_code=500, code="invoices_table_missing", message="Invoice table is not available.")
    invoice_items_relation = _business_invoice_items_relation(conn)
    invoice_payments_relation = _business_invoice_payments_relation(conn)
    invoice_documents_relation = _business_invoice_documents_relation(conn)
    customer_relation = _business_customers_relation(conn)
    if not customer_relation:
        customer_relation = "(select null::uuid as id, null::text as name, null::text as phone, null::uuid as user_id, null::uuid as profile_id) as customers_empty"
    with conn.cursor() as cur:
        cur.execute(
            f"""
            select i.*, c.id as customer_id_join, c.name as customer_name_join, c.phone as customer_phone_join
            from {invoices_relation} i
            left join {customer_relation} c on c.id = i.customer_id and c.user_id = i.user_id and c.profile_id = i.profile_id
            where i.id = %(invoice_id)s and i.user_id = %(user_id)s
            limit 1
            """,
            {"invoice_id": invoice_id, "user_id": auth.user_id},
        )
        invoice = cur.fetchone()
        if not invoice:
            raise ApiError(status_code=404, code="invoice_not_found", message="Invoice not found.")

        if invoice_items_relation:
            cur.execute(
                f"""
                select *
                from {invoice_items_relation}
                where invoice_id = %(invoice_id)s and user_id = %(user_id)s
                order by created_at asc
                """,
                {"invoice_id": invoice_id, "user_id": auth.user_id},
            )
            items = cur.fetchall() or []
        else:
            items = []

        if invoice_payments_relation:
            cur.execute(
                f"""
                select *
                from {invoice_payments_relation}
                where invoice_id = %(invoice_id)s and user_id = %(user_id)s
                order by date desc, created_at desc
                """,
                {"invoice_id": invoice_id, "user_id": auth.user_id},
            )
            payments = cur.fetchall() or []
        else:
            payments = []

        if invoice_documents_relation:
            cur.execute(
                f"""
                select *
                from {invoice_documents_relation}
                where invoice_id = %(invoice_id)s and user_id = %(user_id)s
                order by created_at desc
                limit 1
                """,
                {"invoice_id": invoice_id, "user_id": auth.user_id},
            )
            document = cur.fetchone()
        else:
            document = None

    customer = None
    if invoice.get("customer_id_join"):
        customer = {
            "id": invoice.get("customer_id_join"),
            "name": invoice.get("customer_name_join"),
            "phone": invoice.get("customer_phone_join"),
        }
    return {"invoice": invoice, "items": items, "payments": payments, "customer": customer, "document": document}


@router.get("/invoices/outstanding")
def list_business_outstanding_invoices(
    profile_id: str = Query(...),
    limit: int = Query(default=30, ge=1, le=1000),
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    apply_db_auth_context(conn, auth.user_id)
    invoices_relation = _business_invoices_relation(conn)
    invoice_items_relation = _business_invoice_items_relation(conn)
    invoice_payments_relation = _business_invoice_payments_relation(conn)
    if not invoices_relation:
        return {"items": []}
    if not invoice_items_relation:
        invoice_items_relation = "(select null::uuid as id, null::uuid as invoice_id, null::text as product_name_snapshot, null::numeric as qty, null::timestamptz as created_at) as invoice_items_empty"
    if not invoice_payments_relation:
        invoice_payments_relation = "(select null::uuid as id, null::uuid as invoice_id, null::text as mode, null::numeric as amount, null::date as date, null::uuid as bank_account_id, null::timestamptz as created_at) as invoice_payments_empty"
    customer_relation = _business_customers_relation(conn)
    if not customer_relation:
        customer_relation = "(select null::uuid as id, null::text as name, null::text as phone, null::uuid as user_id, null::uuid as profile_id) as customers_empty"
    with conn.cursor() as cur:
        cur.execute(
            f"""
            select
              i.*,
              c.id as customer_id_join,
              c.name as customer_name_join,
              c.phone as customer_phone_join,
              coalesce(
                json_agg(distinct jsonb_build_object('id', ii.id, 'product_name_snapshot', ii.product_name_snapshot, 'qty', ii.qty))
                filter (where ii.id is not null),
                '[]'::json
              ) as items,
              coalesce(
                json_agg(distinct jsonb_build_object('id', ip.id, 'mode', ip.mode, 'amount', ip.amount, 'date', ip.date, 'bank_account_id', ip.bank_account_id, 'created_at', ip.created_at))
                filter (where ip.id is not null),
                '[]'::json
            ) as payments
            from {invoices_relation} i
            left join {customer_relation} c on c.id = i.customer_id and c.user_id = i.user_id and c.profile_id = i.profile_id
            left join {invoice_items_relation} ii on ii.invoice_id = i.id
            left join {invoice_payments_relation} ip on ip.invoice_id = i.id
            where i.user_id = %(user_id)s
              and i.profile_id = %(profile_id)s
              and i.payment_status in ('partial', 'due')
            group by i.id, c.id
            order by i.date desc, i.created_at desc
            limit %(limit)s
            """,
            {"user_id": auth.user_id, "profile_id": profile_id, "limit": limit},
        )
        rows = cur.fetchall() or []
    return {"items": rows}


@router.get("/invoices/customer-payment-history")
def get_business_customer_payment_history(
    profile_id: str = Query(...),
    customer_ids: str = Query(...),
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    apply_db_auth_context(conn, auth.user_id)
    invoices_relation = _business_invoices_relation(conn)
    invoice_payments_relation = _business_invoice_payments_relation(conn)
    if not invoices_relation or not invoice_payments_relation:
        return {"items": []}
    ids = [part.strip() for part in customer_ids.split(",") if part.strip()]
    if not ids:
        return {"items": []}
    with conn.cursor() as cur:
        cur.execute(
            f"""
            select
              i.customer_id,
              ip.id,
              ip.mode,
              ip.amount,
              ip.date,
              ip.created_at
            from {invoices_relation} i
            join {invoice_payments_relation} ip on ip.invoice_id = i.id
            where i.user_id = %(user_id)s
              and i.profile_id = %(profile_id)s
              and i.customer_id = any(%(customer_ids)s::uuid[])
            order by ip.date desc, ip.created_at desc
            """,
            {
                "user_id": auth.user_id,
                "profile_id": profile_id,
                "customer_ids": ids,
            },
        )
        items = cur.fetchall() or []
    return {"items": items}


@router.post("/invoice-documents/upsert")
def upsert_business_invoice_document(
    payload: dict,
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    profile_id = str(payload.get("profile_id") or "").strip()
    invoice_id = str(payload.get("invoice_id") or "").strip()
    if not profile_id or not invoice_id:
        raise ApiError(status_code=400, code="invalid_payload", message="profile_id and invoice_id are required.")
    apply_db_auth_context(conn, auth.user_id)
    invoice_documents_relation = _business_invoice_documents_relation(conn)
    if not invoice_documents_relation:
        raise ApiError(status_code=500, code="invoice_documents_table_missing", message="Invoice document table is not available.")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            insert into {invoice_documents_relation} (
              user_id, profile_id, invoice_id, storage_path, file_name, mime_type, template_version, file_size_bytes
            ) values (
              %(user_id)s, %(profile_id)s, %(invoice_id)s, %(storage_path)s, %(file_name)s, %(mime_type)s, %(template_version)s, %(file_size_bytes)s
            )
            on conflict (invoice_id, template_version)
            do update set
              storage_path = excluded.storage_path,
              file_name = excluded.file_name,
              mime_type = excluded.mime_type,
              file_size_bytes = excluded.file_size_bytes
            returning *
            """,
            {
                "user_id": auth.user_id,
                "profile_id": profile_id,
                "invoice_id": invoice_id,
                "storage_path": payload.get("storage_path"),
                "file_name": payload.get("file_name"),
                "mime_type": payload.get("mime_type") or "application/pdf",
                "template_version": payload.get("template_version") or "invoice_a4_v1",
                "file_size_bytes": payload.get("file_size_bytes"),
            },
        )
        item = cur.fetchone()
    if not item:
        raise ApiError(status_code=500, code="invoice_document_upsert_failed", message="Could not upsert invoice document.")
    return {"item": item}


def _upload_to_supabase_storage(
    *,
    auth: AuthContext,
    bucket: str,
    file_path: str,
    content_type: str,
    raw_bytes: bytes,
    upsert: bool,
) -> dict:
    from app.core.config import get_settings

    settings = get_settings()
    if not settings.supabase_anon_key:
        raise ApiError(
            status_code=500,
            code="missing_supabase_anon_key",
            message="SUPABASE_ANON_KEY is required for backend storage upload proxy.",
        )

    encoded_path = quote(file_path, safe="/-_.")
    url = f"{settings.supabase_url.rstrip('/')}/storage/v1/object/{bucket}/{encoded_path}"
    headers = {
        "Authorization": f"Bearer {auth.access_token}",
        "apikey": settings.supabase_anon_key,
        "Content-Type": content_type,
        "x-upsert": "true" if upsert else "false",
    }
    request = Request(url, data=raw_bytes, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=30):
            pass
    except Exception as exc:
        raise ApiError(
            status_code=502,
            code="storage_upload_failed",
            message=f"Storage upload failed: {exc}",
        ) from exc

    public_url = (
        f"{settings.supabase_url.rstrip('/')}/storage/v1/object/public/{bucket}/{encoded_path}"
    )
    return {"path": file_path, "public_url": public_url}


@router.post("/storage/upload")
def upload_business_storage_object(
    payload: dict,
    auth: AuthContext = Depends(get_auth_context),
) -> dict:
    bucket = str(payload.get("bucket") or "").strip()
    file_path = str(payload.get("file_path") or "").strip()
    content_type = str(payload.get("content_type") or "application/octet-stream").strip()
    base64_data = str(payload.get("base64_data") or "").strip()
    upsert = bool(payload.get("upsert"))

    if bucket not in {"ledger-attachments", "invoice-pdfs", "avatars"}:
        raise ApiError(status_code=400, code="invalid_bucket", message="Bucket is not allowed.")
    if not file_path:
        raise ApiError(status_code=400, code="invalid_path", message="file_path is required.")
    if not base64_data:
        raise ApiError(status_code=400, code="invalid_payload", message="base64_data is required.")

    try:
        raw_bytes = base64.b64decode(base64_data, validate=True)
    except Exception as exc:
        raise ApiError(status_code=400, code="invalid_base64", message="Invalid base64_data payload.") from exc

    return _upload_to_supabase_storage(
        auth=auth,
        bucket=bucket,
        file_path=file_path,
        content_type=content_type,
        raw_bytes=raw_bytes,
        upsert=upsert,
    )


@router.post("/storage/remove")
def remove_business_storage_object(
    payload: dict,
    auth: AuthContext = Depends(get_auth_context),
) -> dict:
    from app.core.config import get_settings

    bucket = str(payload.get("bucket") or "").strip()
    file_path = str(payload.get("file_path") or "").strip()
    if bucket not in {"ledger-attachments", "invoice-pdfs", "avatars"}:
        raise ApiError(status_code=400, code="invalid_bucket", message="Bucket is not allowed.")
    if not file_path:
        raise ApiError(status_code=400, code="invalid_path", message="file_path is required.")

    settings = get_settings()
    if not settings.supabase_anon_key:
        raise ApiError(
            status_code=500,
            code="missing_supabase_anon_key",
            message="SUPABASE_ANON_KEY is required for backend storage remove proxy.",
        )

    encoded_path = quote(file_path, safe="/-_.")
    url = f"{settings.supabase_url.rstrip('/')}/storage/v1/object/{bucket}/{encoded_path}"
    headers = {
        "Authorization": f"Bearer {auth.access_token}",
        "apikey": settings.supabase_anon_key,
    }
    request = Request(url, headers=headers, method="DELETE")
    try:
        with urlopen(request, timeout=20):
            pass
    except Exception:
        # Ignore remove failures and keep profile update flow resilient.
        return {"ok": False}
    return {"ok": True}
