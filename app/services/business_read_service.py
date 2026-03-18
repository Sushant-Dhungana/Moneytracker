from __future__ import annotations

import base64
from datetime import date, timedelta
from typing import Literal

from psycopg import Connection

from app.core.errors import ApiError
from app.services.ai_business_service import validate_business_profile_ownership
from app.services.ai_business_vector_service import _first_existing_relation

TransactionSection = Literal["posting", "customer", "supplier"]


_BUSINESS_CATEGORY_DOMAINS = {"product", "customer", "supplier", "income", "expense"}


def _relation_has_column(conn: Connection, relation: str | None, column_name: str) -> bool:
    if not relation or "." not in relation:
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


def _existing_relations(conn: Connection, relations: list[str]) -> list[str]:
    existing: list[str] = []
    with conn.cursor() as cur:
        for relation in relations:
            cur.execute("select to_regclass(%(relation)s) as rel", {"relation": relation})
            row = cur.fetchone() or {}
            if row.get("rel"):
                existing.append(relation)
    return existing


def _function_exists(conn: Connection, signature: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("select to_regprocedure(%(signature)s) as fn", {"signature": signature})
        row = cur.fetchone() or {}
    return bool(row.get("fn"))


def _business_accounts_relation(conn: Connection) -> str | None:
    return _first_existing_relation(conn, ["shared.accounts", "business.accounts", "public.accounts"])


def _business_customers_relation(conn: Connection) -> str | None:
    return _first_existing_relation(conn, ["business.customers", "public.customers"])


def _business_invoices_relation(conn: Connection) -> str | None:
    return _first_existing_relation(conn, ["business.invoices", "public.invoices"])


def _business_invoice_items_relation(conn: Connection) -> str | None:
    return _first_existing_relation(conn, ["business.invoice_items", "public.invoice_items"])


def _business_invoice_payments_relation(conn: Connection) -> str | None:
    return _first_existing_relation(conn, ["business.invoice_payments", "public.invoice_payments"])


def _business_products_relation(conn: Connection) -> str | None:
    return _first_existing_relation(conn, ["business.products", "public.products"])


def _business_ledger_postings_relation(conn: Connection) -> str | None:
    return _first_existing_relation(conn, ["business.ledger_postings", "public.ledger_postings"])


def _business_ledger_entries_relation(conn: Connection) -> str | None:
    return _first_existing_relation(conn, ["business.ledger_entries", "public.ledger_entries"])


def _build_business_account_current_balance_sql(conn: Connection, *, active_only: bool = False) -> str:
    accounts_relation = _business_accounts_relation(conn)
    ledger_postings_relation = _business_ledger_postings_relation(conn)
    has_opening_balance = _relation_has_column(conn, accounts_relation, "opening_balance")
    has_current_balance = _relation_has_column(conn, accounts_relation, "current_balance")
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


def _has_unified_business_categories(conn: Connection) -> bool:
    with conn.cursor() as cur:
        cur.execute("select to_regclass('public.business_categories') as rel")
        row = cur.fetchone() or {}
    return bool(row.get("rel"))


def _legacy_category_relation_for_domain(conn: Connection, domain: str) -> str | None:
    normalized = str(domain or "").strip().lower()
    if normalized not in _BUSINESS_CATEGORY_DOMAINS:
        return None
    if normalized == "product":
        return _first_existing_relation(conn, ["business.product_categories", "public.product_categories"])
    if normalized == "customer":
        return _first_existing_relation(conn, ["business.customer_categories", "public.customer_categories"])
    if normalized == "supplier":
        return _first_existing_relation(conn, ["business.supplier_categories", "public.supplier_categories"])
    if normalized in {"income", "expense"}:
        return _first_existing_relation(conn, ["personal.categories", "public.categories"])
    return None


def _encode_offset_cursor(offset: int) -> str:
    raw = str(max(0, int(offset))).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_offset_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        decoded = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        parsed = int(decoded)
    except Exception as exc:  # pragma: no cover - invalid client cursor
        raise ApiError(
            status_code=400,
            code="invalid_cursor",
            message="Invalid cursor provided.",
        ) from exc
    return max(0, parsed)


def _resolve_feed_date_range(
    *,
    period: str | None,
    from_date: date | None,
    to_date: date | None,
) -> tuple[date | None, date | None, str]:
    normalized = str(period or "all").strip().lower()
    today = date.today()

    if normalized in {"all", "all_time"}:
        return None, None, "all"
    if normalized in {"day", "today"}:
        return today, today, "day"
    if normalized == "week":
        start = today - timedelta(days=today.weekday())
        return start, start + timedelta(days=6), "week"
    if normalized == "month":
        start = today.replace(day=1)
        if start.month == 12:
            next_month = start.replace(year=start.year + 1, month=1, day=1)
        else:
            next_month = start.replace(month=start.month + 1, day=1)
        return start, next_month - timedelta(days=1), "month"
    if normalized == "year":
        start = date(today.year, 1, 1)
        end = date(today.year, 12, 31)
        return start, end, "year"
    if normalized == "range":
        if not from_date:
            raise ApiError(
                status_code=400,
                code="missing_from_date",
                message="from date is required when period=range.",
            )
        end = to_date or from_date
        if from_date > end:
            from_date, end = end, from_date
        return from_date, end, "range"
    raise ApiError(
        status_code=400,
        code="invalid_period",
        message="period must be one of all, day, week, month, year, or range.",
    )


def _parse_number(value: object) -> float:
    try:
        parsed = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return parsed if parsed == parsed else 0.0


def _normalize_string(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def fetch_business_product_sales_summary(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    product_id: str,
    period: str | None,
    from_date: date | None,
    to_date: date | None,
) -> dict:
    validate_business_profile_ownership(conn, user_id=user_id, profile_id=profile_id)
    normalized_product_id = str(product_id or "").strip()
    if not normalized_product_id:
        raise ApiError(
            status_code=400,
            code="invalid_product_id",
            message="product_id is required.",
        )

    resolved_from, resolved_to, normalized_period = _resolve_feed_date_range(
        period=period,
        from_date=from_date,
        to_date=to_date,
    )

    invoices_relation = _business_invoices_relation(conn)
    invoice_items_relation = _business_invoice_items_relation(conn)
    products_relation = _business_products_relation(conn)

    product_name: str | None = None
    if products_relation:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                select name
                from {products_relation}
                where id = %(product_id)s::uuid
                  and user_id = %(user_id)s::uuid
                  and profile_id = %(profile_id)s::uuid
                limit 1
                """,
                {
                    "product_id": normalized_product_id,
                    "user_id": user_id,
                    "profile_id": profile_id,
                },
            )
            row = cur.fetchone() or {}
            product_name = _normalize_string(row.get("name"))

    if not invoices_relation or not invoice_items_relation:
        return {
            "product_id": normalized_product_id,
            "product_name": product_name or "Product",
            "period": normalized_period,
            "from_date": resolved_from.isoformat() if resolved_from else None,
            "to_date": resolved_to.isoformat() if resolved_to else None,
            "qty_sold": 0.0,
            "sales_amount": 0.0,
            "invoice_count": 0,
        }

    has_product_id = _relation_has_column(conn, invoice_items_relation, "product_id")
    has_qty = _relation_has_column(conn, invoice_items_relation, "qty")
    has_rate = _relation_has_column(conn, invoice_items_relation, "rate")
    has_total = _relation_has_column(conn, invoice_items_relation, "total")
    has_product_name_snapshot = _relation_has_column(
        conn, invoice_items_relation, "product_name_snapshot"
    )

    if not has_product_id:
        return {
            "product_id": normalized_product_id,
            "product_name": product_name or "Product",
            "period": normalized_period,
            "from_date": resolved_from.isoformat() if resolved_from else None,
            "to_date": resolved_to.isoformat() if resolved_to else None,
            "qty_sold": 0.0,
            "sales_amount": 0.0,
            "invoice_count": 0,
        }

    qty_expr = "coalesce(ii.qty, 0)::numeric" if has_qty else "0::numeric"
    if has_total:
        sales_expr = "coalesce(ii.total, 0)::numeric"
    elif has_qty and has_rate:
        sales_expr = "(coalesce(ii.qty, 0) * coalesce(ii.rate, 0))::numeric"
    elif has_rate:
        sales_expr = "coalesce(ii.rate, 0)::numeric"
    else:
        sales_expr = "0::numeric"
    snapshot_name_expr = (
        "max(nullif(ii.product_name_snapshot, ''))"
        if has_product_name_snapshot
        else "null::text"
    )

    bind: dict[str, object] = {
        "user_id": user_id,
        "profile_id": profile_id,
        "product_id": normalized_product_id,
    }
    where_parts = [
        "i.user_id = %(user_id)s::uuid",
        "i.profile_id = %(profile_id)s::uuid",
        "ii.product_id = %(product_id)s::uuid",
    ]
    if resolved_from:
        where_parts.append("i.date >= %(from_date)s")
        bind["from_date"] = resolved_from
    if resolved_to:
        where_parts.append("i.date <= %(to_date)s")
        bind["to_date"] = resolved_to

    with conn.cursor() as cur:
        cur.execute(
            f"""
            select
              coalesce(sum({qty_expr}), 0) as qty_sold,
              coalesce(sum({sales_expr}), 0) as sales_amount,
              count(distinct i.id) as invoice_count,
              {snapshot_name_expr} as product_name_snapshot
            from {invoices_relation} i
            join {invoice_items_relation} ii on ii.invoice_id = i.id
            where {' and '.join(where_parts)}
            """,
            bind,
        )
        row = cur.fetchone() or {}

    resolved_name = (
        _normalize_string(row.get("product_name_snapshot"))
        or product_name
        or "Product"
    )
    return {
        "product_id": normalized_product_id,
        "product_name": resolved_name,
        "period": normalized_period,
        "from_date": resolved_from.isoformat() if resolved_from else None,
        "to_date": resolved_to.isoformat() if resolved_to else None,
        "qty_sold": round(_parse_number(row.get("qty_sold")), 3),
        "sales_amount": round(_parse_number(row.get("sales_amount")), 2),
        "invoice_count": int(row.get("invoice_count") or 0),
    }


def _transaction_title(txn_type: str) -> str:
    mapping = {
        "sale": "Sale",
        "income": "Income",
        "expense": "Expense",
        "inventory_in": "Purchase",
        "inventory_opening": "Opening Inventory",
        "receivable_collection": "Receivable Collection",
        "transfer": "Transfer",
        "opening_account": "Opening Balance",
        "product_added": "Product Added",
        "adjustment": "Adjustment",
    }
    return mapping.get(str(txn_type or "").strip().lower(), "Adjustment")


def _read_metadata_string(metadata: object, key: str) -> str | None:
    if not isinstance(metadata, dict):
        return None
    return _normalize_string(metadata.get(key))


def _read_metadata_number(metadata: object, key: str) -> float | None:
    if not isinstance(metadata, dict):
        return None
    value = metadata.get(key)
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed else None


def _supplier_name_from_description(description: str | None) -> str | None:
    if not description:
        return None
    lowered = description.lower()
    if "supplier:" in lowered:
        raw = description.split("supplier:", 1)[1].split("|", 1)[0].strip()
        return raw or None
    if "party:" in lowered:
        raw = description.split("party:", 1)[1].split("|", 1)[0].strip()
        return raw or None
    return None


def _map_feed_item(row: dict) -> dict:
    txn_type = str(row.get("txn_type") or "adjustment").strip().lower()
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else None
    customer_name = _normalize_string(row.get("customer_name"))
    supplier_name = _normalize_string(row.get("supplier_name")) or _supplier_name_from_description(
        _normalize_string(row.get("description"))
    )
    account_name = _normalize_string(row.get("account_name"))
    invoice_products = [
        str(item).strip()
        for item in (row.get("invoice_product_names") or [])
        if str(item).strip()
    ]
    if txn_type == "product_added":
        product_name = _read_metadata_string(metadata, "product_name") or _normalize_string(
            row.get("product_name")
        )
        context_text = f"Product: {product_name}" if product_name else None
    elif txn_type in {"inventory_in", "adjustment"}:
        product_name = _read_metadata_string(metadata, "product_name")
        if product_name and supplier_name:
            context_text = f"Product: {product_name} • Supplier: {supplier_name}"
        elif product_name:
            context_text = f"Product: {product_name}"
        elif supplier_name:
            context_text = f"Supplier: {supplier_name}"
        else:
            context_text = None
    elif txn_type == "sale" and invoice_products:
        visible = ", ".join(invoice_products[:2])
        suffix = f" +{len(invoice_products) - 2}" if len(invoice_products) > 2 else ""
        products_text = f"Products: {visible}{suffix}"
        context_text = f"Customer: {customer_name} • {products_text}" if customer_name else products_text
    elif txn_type in {"sale", "receivable_collection"} and customer_name:
        context_text = f"Customer: {customer_name}"
    else:
        context_text = None

    if account_name:
        account_tag_label = account_name
    elif txn_type == "transfer":
        account_tag_label = "Account Transfer"
    elif txn_type in {"sale", "receivable_collection"}:
        account_tag_label = customer_name or "Receivable"
    elif txn_type in {"inventory_in", "expense"}:
        account_tag_label = supplier_name or "Payable"
    else:
        account_tag_label = "Unassigned"

    amount = _parse_number(row.get("amount"))
    is_negative = txn_type in {"inventory_in", "expense", "transfer"}
    signed_amount = -abs(amount) if is_negative else abs(amount)
    payment_mode = str(row.get("payment_mode") or "").strip().lower()
    invoice_status = str(row.get("invoice_payment_status") or "").strip().lower()
    description_text = str(row.get("description") or "").lower()
    due_amount = _read_metadata_number(metadata, "due_amount")
    is_due = bool(
        txn_type in {"sale", "receivable_collection"}
        and (
            (due_amount is not None and due_amount > 0)
            or payment_mode in {"credit", "partial"}
            or invoice_status in {"partial", "due"}
            or "due" in description_text
        )
    )
    product_details = None
    if txn_type == "product_added":
        product_details = {
            "sku": _read_metadata_string(metadata, "sku"),
            "quantity": _parse_number(_read_metadata_number(metadata, "quantity")),
            "unit_cost": _parse_number(_read_metadata_number(metadata, "unit_cost")),
        }

    return {
        "id": str(row.get("id") or ""),
        "entry_id": _normalize_string(row.get("entry_id")),
        "txn_type": txn_type,
        "amount": round(amount, 2),
        "signed_amount": round(signed_amount, 2),
        "date": str(row.get("tx_date") or row.get("date") or ""),
        "created_at": str(row.get("created_at") or ""),
        "title": _transaction_title(txn_type),
        "context_text": context_text,
        "description": _normalize_string(row.get("description")),
        "account_tag_label": account_tag_label,
        "is_due": is_due,
        "section": str(row.get("section") or "posting"),
        "expandable": bool(row.get("entry_id")) and txn_type != "product_added",
        "product_added_details": product_details,
    }


def fetch_business_transactions_feed(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    limit: int,
    cursor: str | None,
    period: str | None,
    from_date: date | None,
    to_date: date | None,
    section: TransactionSection | None,
    search: str | None,
) -> dict:
    validate_business_profile_ownership(conn, user_id=user_id, profile_id=profile_id)
    offset = _decode_offset_cursor(cursor)
    page_size = max(1, min(limit, 100))
    resolved_from, resolved_to, normalized_period = _resolve_feed_date_range(
        period=period,
        from_date=from_date,
        to_date=to_date,
    )

    entries_relations = _existing_relations(conn, ["business.ledger_entries", "public.ledger_entries"])
    products_relation = _business_products_relation(conn)
    customers_relation = _business_customers_relation(conn)
    accounts_relation = _business_accounts_relation(conn)
    invoices_relation = _business_invoices_relation(conn)
    invoice_items_relation = _business_invoice_items_relation(conn)

    if not entries_relations and not products_relation:
        return {
            "items": [],
            "next_cursor": None,
            "has_more": False,
            "posting_count": 0,
            "customer_count": 0,
            "supplier_count": 0,
            "period": normalized_period,
            "from_date": resolved_from.isoformat() if resolved_from else None,
            "to_date": resolved_to.isoformat() if resolved_to else None,
        }

    has_entry_metadata_any = any(
        _relation_has_column(conn, entries_relation, "metadata")
        for entries_relation in entries_relations
    )
    has_product_sku = _relation_has_column(conn, products_relation, "sku") if products_relation else False
    has_product_active = _relation_has_column(conn, products_relation, "is_active") if products_relation else False
    has_invoice_payment_status = _relation_has_column(conn, invoices_relation, "payment_status") if invoices_relation else False
    has_invoice_item_product_name_snapshot = (
        _relation_has_column(conn, invoice_items_relation, "product_name_snapshot")
        if invoice_items_relation
        else False
    )

    invoice_items_join = "left join lateral (select null::text[] as product_names) invoice_items_agg on true"
    if invoice_items_relation and has_entry_metadata_any and has_invoice_item_product_name_snapshot:
        invoice_items_join = f"""
        left join lateral (
          select array_remove(array_agg(distinct nullif(ii.product_name_snapshot, '') order by nullif(ii.product_name_snapshot, '')), null) as product_names
          from {invoice_items_relation} ii
          where ii.invoice_id::text = nullif(le.metadata ->> 'invoice_id', '')
        ) invoice_items_agg on true
        """

    invoice_join = "left join lateral (select null::text as payment_status) invoice_join on true"
    if invoices_relation and has_entry_metadata_any and has_invoice_payment_status:
        invoice_join = f"""
        left join lateral (
          select i.payment_status::text as payment_status
          from {invoices_relation} i
          where i.user_id = %(user_id)s::uuid
            and i.profile_id = %(profile_id)s::uuid
            and i.id::text = nullif(le.metadata ->> 'invoice_id', '')
          limit 1
        ) invoice_join on true
        """

    ledger_cte = "select null::text as id, null::text as entry_id, null::text as txn_type, 0::numeric as amount, null::date as tx_date, null::timestamptz as created_at, null::text as description, null::jsonb as metadata, null::text as account_name, null::text as customer_name, null::text as supplier_name, null::text[] as invoice_product_names, null::text as payment_mode, null::text as invoice_payment_status, 'supplier'::text as section where false"
    ledger_cte_for_counts = ledger_cte
    if entries_relations:
        ledger_count_parts: list[str] = []
        ledger_page_parts: list[str] = []
        for entries_relation in entries_relations:
            has_entry_counterparty = _relation_has_column(conn, entries_relation, "counterparty_id")
            has_entry_metadata = _relation_has_column(conn, entries_relation, "metadata")
            metadata_select = "le.metadata" if has_entry_metadata else "null::jsonb"
            metadata_supplier_sql = "coalesce(le.metadata ->> 'supplier_name', le.metadata ->> 'party_name')" if has_entry_metadata else "null::text"
            metadata_payment_mode_sql = "nullif(le.metadata ->> 'payment_mode', '')" if has_entry_metadata else "null::text"
            counterparty_select = "le.counterparty_id" if has_entry_counterparty else "null::uuid"
            ledger_date_filters = []
            if resolved_from:
                ledger_date_filters.append("le.date >= %(from_date)s")
            if resolved_to:
                ledger_date_filters.append("le.date <= %(to_date)s")
            ledger_date_sql = f"and {' and '.join(ledger_date_filters)}" if ledger_date_filters else ""
            ledger_search_sql = ""
            if search:
                ledger_search_sql = """
                and (
                  le.description ilike %(search)s
                  or coalesce(a.name, '') ilike %(search)s
                  or coalesce(c.name, '') ilike %(search)s
                  or coalesce(invoice_join.payment_status, '') ilike %(search)s
                  or coalesce(array_to_string(invoice_items_agg.product_names, ' '), '') ilike %(search)s
                  or coalesce(nullif(le.metadata ->> 'supplier_name', ''), '') ilike %(search)s
                  or coalesce(nullif(le.metadata ->> 'party_name', ''), '') ilike %(search)s
                  or coalesce(nullif(le.metadata ->> 'product_name', ''), '') ilike %(search)s
                )
                """ if has_entry_metadata else """
                and (
                  le.description ilike %(search)s
                  or coalesce(a.name, '') ilike %(search)s
                  or coalesce(c.name, '') ilike %(search)s
                )
                """
            metadata_transfer_sql = "coalesce(le.metadata ->> 'operation', '') = 'account_transfer'" if has_entry_metadata else "false"
            transfer_match_expr = f"(le.txn_type = 'transfer' or ({metadata_transfer_sql}))"
            txn_type_expr = f"case when {transfer_match_expr} then 'transfer' else le.txn_type::text end"
            section_expr = (
                f"case when {transfer_match_expr} then 'posting' "
                f"when ({txn_type_expr}) in ('sale', 'receivable_collection') then 'customer' "
                "when c.name is not null then 'customer' "
                f"when nullif(coalesce({metadata_supplier_sql}, ''), '') is not null then 'supplier' "
                f"when ({txn_type_expr}) in ('inventory_in', 'inventory_opening', 'product_added') then 'supplier' "
                f"when ({txn_type_expr}) in ('income', 'expense') then 'posting' "
                "else 'posting' end"
            )
            ledger_section_sql = f"and {section_expr} = %(section)s" if section else ""
            ledger_common_filters_sql = "\n          ".join(
                clause for clause in (ledger_date_sql, ledger_search_sql) if clause
            )
            if ledger_common_filters_sql:
                ledger_common_filters_sql = f"\n          {ledger_common_filters_sql}"
            ledger_filtered_filters_sql = "\n          ".join(
                clause for clause in (ledger_date_sql, ledger_section_sql, ledger_search_sql) if clause
            )
            if ledger_filtered_filters_sql:
                ledger_filtered_filters_sql = f"\n          {ledger_filtered_filters_sql}"
            customer_join = "left join lateral (select null::text as name) c on true"
            if customers_relation:
                customer_join = f"left join {customers_relation} c on c.id = {counterparty_select} and c.user_id = le.user_id and c.profile_id = le.profile_id"
            account_join = "left join lateral (select null::text as name) a on true"
            if accounts_relation:
                account_join = f"left join {accounts_relation} a on a.id = le.account_id and a.user_id = le.user_id and a.profile_id = le.profile_id"
            ledger_count_parts.append(
                f"""
                select
                  le.id::text as id,
                  le.id::text as entry_id,
                  {txn_type_expr} as txn_type,
                  coalesce(le.amount, 0)::numeric as amount,
                  le.date::date as tx_date,
                  le.created_at,
                  le.description,
                  {metadata_select} as metadata,
                  a.name as account_name,
                  c.name as customer_name,
                  {metadata_supplier_sql} as supplier_name,
                  invoice_items_agg.product_names as invoice_product_names,
                  {metadata_payment_mode_sql} as payment_mode,
                  invoice_join.payment_status as invoice_payment_status,
                  {section_expr} as section
                from {entries_relation} le
                {account_join}
                {customer_join}
                {invoice_join}
                {invoice_items_join}
                where le.user_id = %(user_id)s::uuid
                  and le.profile_id = %(profile_id)s::uuid
                  and le.txn_type <> 'opening_equity'
                  {ledger_common_filters_sql}
                """
            )
            ledger_page_parts.append(
                f"""
                select
                  le.id::text as id,
                  le.id::text as entry_id,
                  {txn_type_expr} as txn_type,
                  coalesce(le.amount, 0)::numeric as amount,
                  le.date::date as tx_date,
                  le.created_at,
                  le.description,
                  {metadata_select} as metadata,
                  a.name as account_name,
                  c.name as customer_name,
                  {metadata_supplier_sql} as supplier_name,
                  invoice_items_agg.product_names as invoice_product_names,
                  {metadata_payment_mode_sql} as payment_mode,
                  invoice_join.payment_status as invoice_payment_status,
                  {section_expr} as section
                from {entries_relation} le
                {account_join}
                {customer_join}
                {invoice_join}
                {invoice_items_join}
                where le.user_id = %(user_id)s::uuid
                  and le.profile_id = %(profile_id)s::uuid
                  and le.txn_type <> 'opening_equity'
                  {ledger_filtered_filters_sql}
                """
            )
        ledger_cte_for_counts = "\n        union\n".join(ledger_count_parts)
        ledger_cte = "\n        union\n".join(ledger_page_parts)

    product_cte = "select null::text as id, null::text as entry_id, null::text as txn_type, 0::numeric as amount, null::date as tx_date, null::timestamptz as created_at, null::text as description, null::jsonb as metadata, null::text as account_name, null::text as customer_name, null::text as supplier_name, null::text[] as invoice_product_names, null::text as payment_mode, null::text as invoice_payment_status, 'supplier'::text as section where false"
    product_cte_for_counts = product_cte

    bind: dict[str, object] = {
        "user_id": user_id,
        "profile_id": profile_id,
        "offset": offset,
        "limit_plus_one": page_size + 1,
    }
    if resolved_from:
        bind["from_date"] = resolved_from
    if resolved_to:
        bind["to_date"] = resolved_to
    if search:
        bind["search"] = f"%{search.strip()}%"
    if section:
        bind["section"] = section

    counts_base_cte = f"""
    with feed_rows as (
      {ledger_cte_for_counts}
      union all
      {product_cte_for_counts}
    )
    """

    base_cte = f"""
    with feed_rows as (
      {ledger_cte}
      union all
      {product_cte}
    )
    """

    counts_sql = counts_base_cte + """
    select
      coalesce(count(*) filter (where section = 'posting'), 0) as posting_count,
      coalesce(count(*) filter (where section = 'customer'), 0) as customer_count,
      coalesce(count(*) filter (where section = 'supplier'), 0) as supplier_count
    from feed_rows
    """

    page_sql = base_cte + """
    select *
    from feed_rows
    order by tx_date desc nulls last, created_at desc nulls last, id desc
    offset %(offset)s
    limit %(limit_plus_one)s
    """

    with conn.cursor() as cur:
        cur.execute(counts_sql, bind)
        counts_row = cur.fetchone() or {}
        cur.execute(page_sql, bind)
        rows = cur.fetchall() or []

    has_more = len(rows) > page_size
    page_rows = rows[:page_size]
    next_cursor = _encode_offset_cursor(offset + page_size) if has_more else None
    return {
        "items": [_map_feed_item(row) for row in page_rows],
        "next_cursor": next_cursor,
        "has_more": has_more,
        "posting_count": int(counts_row.get("posting_count") or 0),
        "customer_count": int(counts_row.get("customer_count") or 0),
        "supplier_count": int(counts_row.get("supplier_count") or 0),
        "period": normalized_period,
        "from_date": resolved_from.isoformat() if resolved_from else None,
        "to_date": resolved_to.isoformat() if resolved_to else None,
    }


def _fetch_business_product_categories(conn: Connection, *, user_id: str, profile_id: str) -> list[dict]:
    if _has_unified_business_categories(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                select id, user_id, profile_id, domain, name, parent_id, is_active, created_at, updated_at
                from public.business_categories
                where user_id = %(user_id)s::uuid
                  and profile_id = %(profile_id)s::uuid
                  and domain = 'product'
                  and is_active = true
                order by created_at asc
                """,
                {"user_id": user_id, "profile_id": profile_id},
            )
            rows = cur.fetchall() or []
            return [
                {
                    "id": str(row.get("id") or ""),
                    "user_id": str(row.get("user_id") or user_id),
                    "profile_id": str(row.get("profile_id") or profile_id),
                    "domain": str(row.get("domain") or "product"),
                    "name": str(row.get("name") or ""),
                    "parent_id": _normalize_string(row.get("parent_id")),
                    "is_active": bool(row.get("is_active", True)),
                    "created_at": str(row.get("created_at") or ""),
                    "updated_at": str(row.get("updated_at") or row.get("created_at") or ""),
                }
                for row in rows
            ]

    relation = _legacy_category_relation_for_domain(conn, "product")
    if not relation:
        return []
    has_profile_id = _relation_has_column(conn, relation, "profile_id")
    has_updated_at = _relation_has_column(conn, relation, "updated_at")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            select
              id,
              user_id,
              {'profile_id' if has_profile_id else '%(profile_id)s::uuid as profile_id'},
              'product'::text as domain,
              name,
              null::uuid as parent_id,
              true as is_active,
              created_at,
              {'updated_at' if has_updated_at else 'created_at'} as updated_at
            from {relation}
            where user_id = %(user_id)s::uuid
              {'and profile_id = %(profile_id)s::uuid' if has_profile_id else ''}
            order by created_at asc
            """,
            {"user_id": user_id, "profile_id": profile_id},
        )
        rows = cur.fetchall() or []
    return [
        {
            "id": str(row.get("id") or ""),
            "user_id": str(row.get("user_id") or user_id),
            "profile_id": str(row.get("profile_id") or profile_id),
            "domain": str(row.get("domain") or "product"),
            "name": str(row.get("name") or ""),
            "parent_id": _normalize_string(row.get("parent_id")),
            "is_active": bool(row.get("is_active", True)),
            "created_at": str(row.get("created_at") or ""),
            "updated_at": str(row.get("updated_at") or row.get("created_at") or ""),
        }
        for row in rows
    ]


def fetch_business_pos_bootstrap(conn: Connection, *, user_id: str, profile_id: str) -> dict:
    validate_business_profile_ownership(conn, user_id=user_id, profile_id=profile_id)
    accounts_relation = _business_accounts_relation(conn)
    with conn.cursor() as cur:
        cur.execute(
            "select * from public.list_business_products(%(profile_id)s::uuid)",
            {"profile_id": profile_id},
        )
        product_rows = cur.fetchall() or []

    product_categories = _fetch_business_product_categories(
        conn,
        user_id=user_id,
        profile_id=profile_id,
    )

    account_rows: list[dict] = []
    if accounts_relation:
        has_institution_name = _relation_has_column(conn, accounts_relation, "institution_name")
        has_account_number = _relation_has_column(conn, accounts_relation, "account_number")
        has_qr_image_url = _relation_has_column(conn, accounts_relation, "qr_image_url")
        has_opening_balance = _relation_has_column(conn, accounts_relation, "opening_balance")
        has_overdraft_limit = _relation_has_column(conn, accounts_relation, "overdraft_limit")
        current_balance_sql = _build_business_account_current_balance_sql(conn)
        with conn.cursor() as cur:
            cur.execute(
                f"""
                select
                  id, user_id, profile_id, name, type,
                  {'institution_name' if has_institution_name else 'null::text as institution_name'},
                  {'account_number' if has_account_number else 'null::text as account_number'},
                  {'qr_image_url' if has_qr_image_url else 'null::text as qr_image_url'},
                  {'opening_balance' if has_opening_balance else 'null::numeric as opening_balance'},
                  {current_balance_sql},
                  {'overdraft_limit' if has_overdraft_limit else 'null::numeric as overdraft_limit'},
                  is_active,
                  created_at
                from {accounts_relation}
                where user_id = %(user_id)s::uuid
                  and profile_id = %(profile_id)s::uuid
                  and type in ('cash', 'bank', 'merchant')
                  and is_active = true
                order by created_at asc
                """,
                {"user_id": user_id, "profile_id": profile_id},
            )
            account_rows = cur.fetchall() or []

    normalized_products = []
    for row in product_rows:
        normalized_products.append(
            {
                "id": str(row.get("id") or ""),
                "user_id": str(row.get("user_id") or user_id),
                "profile_id": str(row.get("profile_id") or profile_id),
                "name": str(row.get("name") or ""),
                "price": _parse_number(row.get("price")),
                "selling_price": _parse_number(row.get("selling_price") or row.get("price")),
                "quantity": _parse_number(row.get("quantity")),
                "unit_id": _normalize_string(row.get("unit_id")),
                "category_id": _normalize_string(row.get("category_id")),
                "sku": _normalize_string(row.get("sku")),
                "is_active": bool(row.get("is_active", True)),
                "created_at": str(row.get("created_at") or ""),
                "updated_at": str(row.get("updated_at") or row.get("created_at") or ""),
                "unit": (
                    {"id": str(row.get("unit_id") or ""), "name": str(row.get("unit_name") or "")}
                    if _normalize_string(row.get("unit_id"))
                    else None
                ),
                "category": (
                    {"id": str(row.get("category_id") or ""), "name": str(row.get("category_name") or "")}
                    if _normalize_string(row.get("category_id"))
                    else None
                ),
            }
        )

    return {
        "products": [item for item in normalized_products if item["is_active"]],
        "product_categories": product_categories,
        "payment_accounts": account_rows,
    }


def search_business_customers(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    query: str,
    limit: int,
) -> list[dict]:
    validate_business_profile_ownership(conn, user_id=user_id, profile_id=profile_id)
    relation = _business_customers_relation(conn)
    if not relation:
        return []
    normalized_query = str(query or "").strip()
    if not normalized_query:
        return []
    safe_limit = max(1, min(limit, 50))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            select *
            from {relation}
            where user_id = %(user_id)s::uuid
              and profile_id = %(profile_id)s::uuid
              and is_active = true
              and (
                name ilike %(starts)s
                or coalesce(phone, '') ilike %(starts)s
                or coalesce(address, '') ilike %(starts)s
                or name ilike %(contains)s
                or coalesce(phone, '') ilike %(contains)s
                or coalesce(address, '') ilike %(contains)s
              )
            order by
              case when name ilike %(starts)s then 0 else 1 end,
              case when coalesce(phone, '') ilike %(starts)s then 0 else 1 end,
              name asc,
              created_at desc
            limit %(limit)s
            """,
            {
                "user_id": user_id,
                "profile_id": profile_id,
                "starts": f"{normalized_query}%",
                "contains": f"%{normalized_query}%",
                "limit": safe_limit,
            },
        )
        rows = cur.fetchall() or []

    normalized_items: list[dict] = []
    for row in rows:
        normalized_items.append(
            {
                "id": str(row.get("id") or ""),
                "user_id": str(row.get("user_id") or user_id),
                "profile_id": str(row.get("profile_id") or profile_id),
                "name": str(row.get("name") or ""),
                "phone": _normalize_string(row.get("phone")),
                "address": _normalize_string(row.get("address")),
                "category_id": _normalize_string(row.get("category_id")),
                "credit_limit": _parse_number(row.get("credit_limit")),
                "opening_balance": (
                    _parse_number(row.get("opening_balance"))
                    if row.get("opening_balance") is not None
                    else None
                ),
                "opening_balance_type": _normalize_string(row.get("opening_balance_type")),
                "reminder_date": _normalize_string(row.get("reminder_date")),
                "is_active": bool(row.get("is_active", True)),
                "created_at": str(row.get("created_at") or ""),
                "updated_at": str(row.get("updated_at") or row.get("created_at") or ""),
            }
        )
    return normalized_items


def fetch_business_customer_history(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    customer_id: str,
    invoice_limit: int,
    payment_limit: int,
) -> dict:
    validate_business_profile_ownership(conn, user_id=user_id, profile_id=profile_id)
    customers_relation = _business_customers_relation(conn)
    if not customers_relation:
        raise ApiError(
            status_code=404,
            code="customer_not_found",
            message="Customer not found for this profile.",
        )

    invoices_relation = _business_invoices_relation(conn)
    invoice_items_relation = _business_invoice_items_relation(conn)
    invoice_payments_relation = _business_invoice_payments_relation(conn)
    ledger_entries_relation = _business_ledger_entries_relation(conn)
    has_entry_counterparty = (
        _relation_has_column(conn, ledger_entries_relation, "counterparty_id")
        if ledger_entries_relation
        else False
    )

    safe_invoice_limit = max(1, min(invoice_limit, 100))
    safe_payment_limit = max(1, min(payment_limit, 100))

    with conn.cursor() as cur:
        cur.execute(
            f"""
            select c.id
            from {customers_relation} c
            where c.id = %(customer_id)s::uuid
              and c.user_id = %(user_id)s::uuid
              and c.profile_id = %(profile_id)s::uuid
              and c.is_active = true
            limit 1
            """,
            {
                "customer_id": customer_id,
                "user_id": user_id,
                "profile_id": profile_id,
            },
        )
        if not cur.fetchone():
            raise ApiError(
                status_code=404,
                code="customer_not_found",
                message="Customer not found for this profile.",
            )

    purchases: list[dict] = []
    if invoices_relation:
        has_due_amount = _relation_has_column(conn, invoices_relation, "due_amount")
        has_paid_amount = _relation_has_column(conn, invoices_relation, "paid_amount")
        has_invoice_item_product_name_snapshot = (
            _relation_has_column(conn, invoice_items_relation, "product_name_snapshot")
            if invoice_items_relation
            else False
        )
        paid_amount_expr = "0::numeric"
        if has_paid_amount:
            paid_amount_expr = "coalesce(i.paid_amount, 0)"
        elif invoice_payments_relation:
            paid_amount_expr = (
                "coalesce((select sum(coalesce(ip.amount, 0))::numeric "
                f"from {invoice_payments_relation} ip "
                "where ip.invoice_id = i.id), 0::numeric)"
            )
        due_amount_expr = (
            f"coalesce(i.due_amount, greatest(coalesce(i.total, 0) - ({paid_amount_expr}), 0))"
            if has_due_amount
            else f"greatest(coalesce(i.total, 0) - ({paid_amount_expr}), 0)"
        )
        invoice_items_join = "left join lateral (select null::text[] as product_names) invoice_items_agg on true"
        if invoice_items_relation and has_invoice_item_product_name_snapshot:
            invoice_items_join = f"""
            left join lateral (
              select array_remove(
                array_agg(distinct nullif(ii.product_name_snapshot, '') order by nullif(ii.product_name_snapshot, '')),
                null
              ) as product_names
              from {invoice_items_relation} ii
              where ii.invoice_id = i.id
            ) invoice_items_agg on true
            """
        with conn.cursor() as cur:
            cur.execute(
                f"""
                select
                  i.id::text as id,
                  coalesce(nullif(i.invoice_no, ''), 'Invoice')::text as invoice_no,
                  coalesce(i.total, 0)::numeric as amount,
                  {due_amount_expr}::numeric as due_amount,
                  i.date::text as date,
                  coalesce(i.payment_status, 'due')::text as payment_status,
                  i.created_at::text as created_at,
                  invoice_items_agg.product_names as product_names
                from {invoices_relation} i
                {invoice_items_join}
                where i.user_id = %(user_id)s::uuid
                  and i.profile_id = %(profile_id)s::uuid
                  and i.customer_id = %(customer_id)s::uuid
                order by i.date desc, i.created_at desc, i.id desc
                limit %(limit)s
                """,
                {
                    "user_id": user_id,
                    "profile_id": profile_id,
                    "customer_id": customer_id,
                    "limit": safe_invoice_limit,
                },
            )
            purchases = [
                {
                    "id": str(row.get("id") or ""),
                    "invoice_no": str(row.get("invoice_no") or "Invoice"),
                    "amount": _parse_number(row.get("amount")),
                    "due_amount": _parse_number(row.get("due_amount")),
                    "date": str(row.get("date") or ""),
                    "payment_status": str(row.get("payment_status") or "due"),
                    "created_at": str(row.get("created_at") or ""),
                    "product_names": [
                        str(item).strip()
                        for item in (row.get("product_names") or [])
                        if str(item).strip()
                    ],
                }
                for row in (cur.fetchall() or [])
            ]

    payment_rows: list[dict] = []
    if invoices_relation and invoice_payments_relation:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                select
                  ip.id::text as id,
                  coalesce(ip.amount, 0)::numeric as amount,
                  ip.date::text as date,
                  nullif(ip.mode::text, '') as mode,
                  'invoice_payment'::text as source,
                  ip.created_at::text as created_at
                from {invoices_relation} i
                join {invoice_payments_relation} ip on ip.invoice_id = i.id
                where i.user_id = %(user_id)s::uuid
                  and i.profile_id = %(profile_id)s::uuid
                  and i.customer_id = %(customer_id)s::uuid
                order by ip.date desc, ip.created_at desc, ip.id desc
                limit %(limit)s
                """,
                {
                    "user_id": user_id,
                    "profile_id": profile_id,
                    "customer_id": customer_id,
                    "limit": safe_payment_limit,
                },
            )
            payment_rows.extend(
                {
                    "id": str(row.get("id") or ""),
                    "amount": _parse_number(row.get("amount")),
                    "date": str(row.get("date") or ""),
                    "mode": _normalize_string(row.get("mode")),
                    "source": "invoice_payment",
                    "created_at": str(row.get("created_at") or ""),
                }
                for row in (cur.fetchall() or [])
            )

    # Fallback: include paid amounts recorded directly on invoices (legacy/partial flows)
    # when invoice_payments rows are absent for that invoice. This keeps customer
    # payment history accurate for partial-paid invoices without duplicating rows.
    if invoices_relation:
        has_invoice_paid_amount = _relation_has_column(conn, invoices_relation, "paid_amount")
        if has_invoice_paid_amount:
            has_invoice_payment_mode = _relation_has_column(conn, invoices_relation, "payment_mode")
            payment_mode_expr = (
                "coalesce(nullif(i.payment_mode::text, ''), 'partial')"
                if has_invoice_payment_mode
                else "'partial'::text"
            )
            missing_invoice_payment_filter = (
                f"""
                and not exists (
                  select 1
                  from {invoice_payments_relation} ip
                  where ip.invoice_id = i.id
                )
                """
                if invoice_payments_relation
                else ""
            )
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    select
                      i.id::text as id,
                      coalesce(i.paid_amount, 0)::numeric as amount,
                      i.date::text as date,
                      {payment_mode_expr} as mode,
                      i.created_at::text as created_at
                    from {invoices_relation} i
                    where i.user_id = %(user_id)s::uuid
                      and i.profile_id = %(profile_id)s::uuid
                      and i.customer_id = %(customer_id)s::uuid
                      and coalesce(i.paid_amount, 0) > 0
                      {missing_invoice_payment_filter}
                    order by i.date desc, i.created_at desc, i.id desc
                    limit %(limit)s
                    """,
                    {
                        "user_id": user_id,
                        "profile_id": profile_id,
                        "customer_id": customer_id,
                        "limit": safe_payment_limit,
                    },
                )
                payment_rows.extend(
                    {
                        "id": f"invoice:{str(row.get('id') or '')}",
                        "amount": _parse_number(row.get("amount")),
                        "date": str(row.get("date") or ""),
                        "mode": _normalize_string(row.get("mode")),
                        "source": "invoice_payment",
                        "created_at": str(row.get("created_at") or ""),
                    }
                    for row in (cur.fetchall() or [])
                )

    if ledger_entries_relation and has_entry_counterparty:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                select
                  le.id::text as id,
                  coalesce(le.amount, 0)::numeric as amount,
                  le.date::text as date,
                  'receivable_collection'::text as source,
                  le.created_at::text as created_at
                from {ledger_entries_relation} le
                where le.user_id = %(user_id)s::uuid
                  and le.profile_id = %(profile_id)s::uuid
                  and le.counterparty_id = %(customer_id)s::uuid
                  and le.txn_type = 'receivable_collection'
                order by le.date desc, le.created_at desc, le.id desc
                limit %(limit)s
                """,
                {
                    "user_id": user_id,
                    "profile_id": profile_id,
                    "customer_id": customer_id,
                    "limit": safe_payment_limit,
                },
            )
            payment_rows.extend(
                {
                    "id": f"ledger:{str(row.get('id') or '')}",
                    "amount": _parse_number(row.get("amount")),
                    "date": str(row.get("date") or ""),
                    "mode": None,
                    "source": "receivable_collection",
                    "created_at": str(row.get("created_at") or ""),
                }
                for row in (cur.fetchall() or [])
            )

    payment_rows.sort(
        key=lambda item: (
            str(item.get("date") or ""),
            str(item.get("created_at") or ""),
            str(item.get("id") or ""),
        ),
        reverse=True,
    )

    return {
        "purchases": purchases,
        "payments": payment_rows[:safe_payment_limit],
    }
