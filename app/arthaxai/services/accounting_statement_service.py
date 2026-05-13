from __future__ import annotations

import re
from datetime import date, timedelta

from psycopg import Connection

from app.arthaxai.services.ai_business_query_service import ParsedDateScope
from app.arthaxai.services.ai_business_vector_service import collect_business_live_snapshot
from app.services.summary_service import _first_existing_relation, _resolve_date_range

_BUSINESS_SCOPE_PATTERN = re.compile(
    r"\b("
    r"business|company|shop|store|customer|supplier|vendor|inventory|stock|invoice|"
    r"receivable|receivables|payable|payables|balance sheet|income statement|"
    r"profit and loss|p&l|cash flow|trial balance|gross profit|accounts receivable|"
    r"accounts payable"
    r")\b",
    re.IGNORECASE,
)

_BUSINESS_FINANCE_PATTERN = re.compile(
    r"\b(balance sheet|income statement|cash flow|equity|inventory|customer due|supplier due|invoice)\b",
    re.IGNORECASE,
)

_FOCUS_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "balance_sheet",
        re.compile(r"\b(balance sheet|financial position|assets and liabilities|net worth)\b", re.IGNORECASE),
    ),
    (
        "income_statement",
        re.compile(r"\b(income statement|profit and loss|p&l|profit|loss|profitability)\b", re.IGNORECASE),
    ),
    (
        "cash_flow",
        re.compile(r"\b(cash flow|cashflow|liquidity|cash position)\b", re.IGNORECASE),
    ),
    (
        "aging",
        re.compile(r"\b(aging|ageing|receivable aging|payable aging|a/r|a p|a/p|a r|overdue)\b", re.IGNORECASE),
    ),
    (
        "ratio_analysis",
        re.compile(r"\b(ratio|ratios|liquidity ratio|current ratio|margin|leverage|efficiency)\b", re.IGNORECASE),
    ),
    (
        "trend_analysis",
        re.compile(r"\b(vertical|horizontal|compare|comparison|trend|variance|vs|versus)\b", re.IGNORECASE),
    ),
]

_PERSONAL_TO_BUSINESS_HANDOFF_MESSAGE = (
    "This is business accounting data. Please switch to Business profile for detailed analysis."
)


def _to_number(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _round_money(value: float) -> float:
    return round(float(value or 0), 2)


def _normalize_label(value: str | None) -> str:
    return str(value or "").strip().lower()


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


def _function_exists(conn: Connection, signature: str) -> bool:
    normalized = str(signature or "").strip()
    if not normalized:
        return False
    with conn.cursor() as cur:
        cur.execute("select to_regprocedure(%(signature)s) as fn", {"signature": normalized})
        row = cur.fetchone() or {}
    return bool(row.get("fn"))


def _build_business_account_current_balance_sql(conn: Connection, *, active_only: bool = False) -> str:
    accounts_relation = _first_existing_relation(conn, ["business.accounts", "public.accounts"])
    ledger_postings_relation = _first_existing_relation(conn, ["business.ledger_postings", "public.ledger_postings"])
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
        opening_balance_expr = (
            "coalesce(opening_balance, 0)::numeric" if has_opening_balance else "0::numeric"
        )
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


def is_business_question_for_personal_chat(query: str) -> bool:
    normalized = str(query or "").strip()
    if not normalized:
        return False
    return bool(_BUSINESS_SCOPE_PATTERN.search(normalized) and _BUSINESS_FINANCE_PATTERN.search(normalized))


def get_personal_to_business_handoff_message() -> str:
    return _PERSONAL_TO_BUSINESS_HANDOFF_MESSAGE


def classify_business_accounting_focus(query: str) -> str:
    normalized = str(query or "").strip()
    for focus, pattern in _FOCUS_PATTERNS:
        if pattern.search(normalized):
            return focus
    return "general_accounting"


def _resolve_statement_scope(scope: ParsedDateScope | None, focus: str) -> tuple[date | None, date | None, str]:
    if scope:
        return scope.start, scope.end, scope.label
    if focus in {"balance_sheet", "cash_flow", "aging", "ratio_analysis", "trend_analysis"}:
        return None, None, "all"
    resolved_from, resolved_to, normalized_period = _resolve_date_range(
        period="month",
        from_date=None,
        to_date=None,
    )
    return resolved_from, resolved_to, normalized_period


def _build_previous_window(start: date | None, end: date | None) -> tuple[date | None, date | None]:
    if not start or not end:
        return None, None
    span_days = max(1, (end - start).days + 1)
    previous_end = start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=span_days - 1)
    return previous_start, previous_end


def _safe_divide(numerator: float, denominator: float) -> float | None:
    if abs(denominator) < 0.000001:
        return None
    return round(numerator / denominator, 4)


def _build_business_posting_totals(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    start_date: date | None,
    end_date: date | None,
) -> dict[str, float]:
    relation_pairs: list[tuple[str, str]] = []
    for postings_candidate, entries_candidate in [
        ("business.ledger_postings", "business.ledger_entries"),
        ("public.ledger_postings", "public.ledger_entries"),
    ]:
        postings_relation = _first_existing_relation(conn, [postings_candidate])
        entries_relation = _first_existing_relation(conn, [entries_candidate])
        if postings_relation and entries_relation:
            relation_pairs.append((postings_relation, entries_relation))

    totals = {
        "sales_revenue": 0.0,
        "other_income": 0.0,
        "operating_expense": 0.0,
        "inventory_added": 0.0,
        # COGS: sum of dedicated cogs/cost_of_goods_sold postings (debit)
        # plus inventory_asset credits (inventory consumed/sold)
        "cogs": 0.0,
        # purchases: total supplier purchase postings (debit side)
        "purchases": 0.0,
    }
    if not relation_pairs:
        return totals

    for postings_relation, entries_relation in relation_pairs:
        bind: dict[str, object] = {
            "user_id": user_id,
            "profile_id": profile_id,
        }
        date_sql = ""
        if start_date:
            date_sql += " and le.date >= %(start_date)s"
            bind["start_date"] = start_date.isoformat()
        if end_date:
            date_sql += " and le.date <= %(end_date)s"
            bind["end_date"] = end_date.isoformat()

        with conn.cursor() as cur:
            cur.execute(
                f"""
                select
                  coalesce(sum(case when lp.leg_type = 'sales_revenue' and lp.direction = 'credit' then lp.amount else 0 end), 0) as sales_revenue,
                  coalesce(sum(case when lp.leg_type = 'income_bucket' and lp.direction = 'credit' then lp.amount else 0 end), 0) as other_income,
                  coalesce(sum(case when lp.leg_type = 'expense_bucket' and lp.direction = 'debit' then lp.amount else 0 end), 0) as operating_expense,
                  coalesce(sum(case when lp.leg_type = 'inventory_asset' and lp.direction = 'debit' then lp.amount else 0 end), 0) as inventory_added,
                  -- COGS: explicit cogs postings OR inventory_asset credits (stock leaving for a sale)
                  coalesce(sum(case
                    when lp.leg_type in ('cogs', 'cost_of_goods_sold') and lp.direction = 'debit' then lp.amount
                    when lp.leg_type = 'inventory_asset' and lp.direction = 'credit' then lp.amount
                    else 0
                  end), 0) as cogs,
                  -- purchases: supplier purchase postings on the expense/payable side
                  coalesce(sum(case
                    when lp.leg_type in ('purchase', 'purchases', 'purchase_expense') and lp.direction = 'debit' then lp.amount
                    else 0
                  end), 0) as purchases
                from {postings_relation} lp
                join {entries_relation} le on le.id = lp.entry_id
                where lp.user_id = %(user_id)s::uuid
                  and lp.profile_id = %(profile_id)s::uuid
                  {date_sql}
                """,
                bind,
            )
            row = cur.fetchone() or {}
        for key in totals:
            totals[key] += _to_number(row.get(key))

    return {key: _round_money(value) for key, value in totals.items()}


def _fetch_payment_accounts(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
) -> list[dict]:
    accounts_relation = _first_existing_relation(conn, ["business.accounts", "public.accounts"])
    if not accounts_relation:
        return []

    current_balance_sql = _build_business_account_current_balance_sql(conn, active_only=False)
    has_overdraft_limit = _relation_has_column(conn, accounts_relation, "overdraft_limit")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            select
              id::text as id,
              coalesce(nullif(trim(name), ''), 'Account') as name,
              lower(trim(type)) as type,
              {current_balance_sql},
              {"coalesce(overdraft_limit, 0)::numeric" if has_overdraft_limit else "0::numeric"} as overdraft_limit
            from {accounts_relation}
            where user_id = %(user_id)s::uuid
              and profile_id = %(profile_id)s::uuid
              and is_active = true
              and type in ('cash', 'bank', 'merchant')
            order by created_at asc
            """,
            {"user_id": user_id, "profile_id": profile_id},
        )
        rows = cur.fetchall() or []
    return rows


def _fetch_inventory_snapshot(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
) -> dict[str, float]:
    products_relation = _first_existing_relation(conn, ["business.products", "public.products"])
    if not products_relation:
        return {"inventory_value_cost": 0.0, "inventory_units_total": 0.0, "product_count": 0.0}

    with conn.cursor() as cur:
        cur.execute(
            f"""
            select
              coalesce(sum(greatest(coalesce(quantity, 0), 0) * greatest(coalesce(price, 0), 0)), 0) as inventory_value_cost,
              coalesce(sum(greatest(coalesce(quantity, 0), 0)), 0) as inventory_units_total,
              coalesce(count(*), 0) as product_count
            from {products_relation}
            where user_id = %(user_id)s::uuid
              and profile_id = %(profile_id)s::uuid
              and is_active = true
            """,
            {"user_id": user_id, "profile_id": profile_id},
        )
        row = cur.fetchone() or {}
    return {
        "inventory_value_cost": _round_money(_to_number(row.get("inventory_value_cost"))),
        "inventory_units_total": _round_money(_to_number(row.get("inventory_units_total"))),
        "product_count": float(row.get("product_count") or 0),
    }


def _fetch_supplier_advance_total(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
) -> float:
    suppliers_relation = _first_existing_relation(conn, ["business.suppliers", "public.suppliers"])
    if not suppliers_relation:
        return 0.0
    has_is_active = _relation_has_column(conn, suppliers_relation, "is_active")
    has_opening_type = _relation_has_column(conn, suppliers_relation, "opening_balance_type")
    has_opening_balance = _relation_has_column(conn, suppliers_relation, "opening_balance")
    if not has_opening_type or not has_opening_balance:
        return 0.0
    with conn.cursor() as cur:
        cur.execute(
            f"""
            select
              coalesce(sum(case when opening_balance_type = 'advance' then opening_balance else 0 end), 0) as advance_total
            from {suppliers_relation}
            where user_id = %(user_id)s::uuid
              and profile_id = %(profile_id)s::uuid
              {"and is_active = true" if has_is_active else ""}
            """,
            {"user_id": user_id, "profile_id": profile_id},
        )
        row = cur.fetchone() or {}
    return _round_money(_to_number(row.get("advance_total")))


def _fetch_cash_movements_by_type(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    start_date: date | None,
    end_date: date | None,
) -> list[dict]:
    postings_relation = _first_existing_relation(conn, ["business.ledger_postings", "public.ledger_postings"])
    entries_relation = _first_existing_relation(conn, ["business.ledger_entries", "public.ledger_entries"])
    if not postings_relation or not entries_relation:
        return []

    bind: dict[str, object] = {
        "user_id": user_id,
        "profile_id": profile_id,
    }
    date_sql = ""
    if start_date:
        date_sql += " and le.date >= %(start_date)s"
        bind["start_date"] = start_date.isoformat()
    if end_date:
        date_sql += " and le.date <= %(end_date)s"
        bind["end_date"] = end_date.isoformat()

    with conn.cursor() as cur:
        cur.execute(
            f"""
            select
              lower(trim(le.txn_type)) as txn_type,
              coalesce(sum(case when lp.direction = 'debit' then lp.amount else -lp.amount end), 0) as net_cash_change
            from {postings_relation} lp
            join {entries_relation} le on le.id = lp.entry_id
            where lp.user_id = %(user_id)s::uuid
              and lp.profile_id = %(profile_id)s::uuid
              and lp.leg_type = 'account'
              {date_sql}
            group by lower(trim(le.txn_type))
            order by lower(trim(le.txn_type)) asc
            """,
            bind,
        )
        return cur.fetchall() or []


def _fetch_general_ledger_activity(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    start_date: date | None,
    end_date: date | None,
    limit: int = 20,
) -> dict[str, object]:
    entries_relation = _first_existing_relation(conn, ["business.ledger_entries", "public.ledger_entries"])
    postings_relation = _first_existing_relation(conn, ["business.ledger_postings", "public.ledger_postings"])
    if not entries_relation:
        return {
            "available": False,
            "scope_summary": {
                "entries_count": 0,
                "gross_amount": 0.0,
                "first_entry_date": None,
                "last_entry_date": None,
            },
            "transaction_type_totals": [],
            "recent_entries": [],
        }

    has_description = _relation_has_column(conn, entries_relation, "description")
    has_amount = _relation_has_column(conn, entries_relation, "amount")
    has_created_at = _relation_has_column(conn, entries_relation, "created_at")
    has_txn_type = _relation_has_column(conn, entries_relation, "txn_type")

    bind: dict[str, object] = {
        "user_id": user_id,
        "profile_id": profile_id,
        "limit": max(1, min(limit, 50)),
    }
    date_sql = ""
    if start_date:
        date_sql += " and le.date >= %(start_date)s"
        bind["start_date"] = start_date.isoformat()
    if end_date:
        date_sql += " and le.date <= %(end_date)s"
        bind["end_date"] = end_date.isoformat()

    description_expr = "coalesce(nullif(trim(le.description), ''), '')" if has_description else "''"
    amount_expr = "coalesce(le.amount, 0)" if has_amount else "0"
    txn_type_expr = "coalesce(nullif(trim(le.txn_type), ''), 'entry')" if has_txn_type else "'entry'"
    created_at_expr = "le.created_at" if has_created_at else "null::timestamptz"

    with conn.cursor() as cur:
        cur.execute(
            f"""
            select
              coalesce(count(*), 0)::int as entries_count,
              coalesce(sum({amount_expr}), 0) as gross_amount,
              min(le.date)::text as first_entry_date,
              max(le.date)::text as last_entry_date
            from {entries_relation} le
            where le.user_id = %(user_id)s::uuid
              and le.profile_id = %(profile_id)s::uuid
              {date_sql}
            """,
            bind,
        )
        summary_row = cur.fetchone() or {}

        cur.execute(
            f"""
            select
              {txn_type_expr} as txn_type,
              coalesce(count(*), 0)::int as entry_count,
              coalesce(sum({amount_expr}), 0) as total_amount
            from {entries_relation} le
            where le.user_id = %(user_id)s::uuid
              and le.profile_id = %(profile_id)s::uuid
              {date_sql}
            group by 1
            order by entry_count desc, total_amount desc, txn_type asc
            limit 12
            """,
            bind,
        )
        txn_type_rows = cur.fetchall() or []

        if postings_relation:
            cur.execute(
                f"""
                select
                  le.id::text as entry_id,
                  le.date::text as entry_date,
                  {txn_type_expr} as txn_type,
                  {description_expr} as description,
                  {amount_expr} as amount,
                  coalesce(sum(case when lp.direction = 'debit' then coalesce(lp.amount, 0) else 0 end), 0) as debit_total,
                  coalesce(sum(case when lp.direction = 'credit' then coalesce(lp.amount, 0) else 0 end), 0) as credit_total,
                  array_remove(array_agg(distinct nullif(lp.leg_type, '') order by nullif(lp.leg_type, '')), null) as leg_types,
                  max({created_at_expr}) as created_at
                from {entries_relation} le
                left join {postings_relation} lp on lp.entry_id = le.id
                where le.user_id = %(user_id)s::uuid
                  and le.profile_id = %(profile_id)s::uuid
                  {date_sql}
                group by le.id, le.date, {txn_type_expr}, {description_expr}, {amount_expr}
                order by le.date desc, created_at desc nulls last, le.id desc
                limit %(limit)s
                """,
                bind,
            )
        else:
            cur.execute(
                f"""
                select
                  le.id::text as entry_id,
                  le.date::text as entry_date,
                  {txn_type_expr} as txn_type,
                  {description_expr} as description,
                  {amount_expr} as amount,
                  0::numeric as debit_total,
                  0::numeric as credit_total,
                  array[]::text[] as leg_types,
                  {created_at_expr} as created_at
                from {entries_relation} le
                where le.user_id = %(user_id)s::uuid
                  and le.profile_id = %(profile_id)s::uuid
                  {date_sql}
                order by le.date desc, {created_at_expr} desc nulls last, le.id desc
                limit %(limit)s
                """,
                bind,
            )
        recent_rows = cur.fetchall() or []

    return {
        "available": True,
        "scope_summary": {
            "entries_count": int(summary_row.get("entries_count") or 0),
            "gross_amount": _round_money(_to_number(summary_row.get("gross_amount"))),
            "first_entry_date": str(summary_row.get("first_entry_date") or "").strip() or None,
            "last_entry_date": str(summary_row.get("last_entry_date") or "").strip() or None,
        },
        "transaction_type_totals": [
            {
                "txn_type": str(row.get("txn_type") or "entry"),
                "entry_count": int(row.get("entry_count") or 0),
                "total_amount": _round_money(_to_number(row.get("total_amount"))),
            }
            for row in txn_type_rows
        ],
        "recent_entries": [
            {
                "entry_id": str(row.get("entry_id") or ""),
                "date": str(row.get("entry_date") or "").strip() or None,
                "txn_type": str(row.get("txn_type") or "entry"),
                "description": str(row.get("description") or "").strip() or None,
                "amount": _round_money(_to_number(row.get("amount"))),
                "debit_total": _round_money(_to_number(row.get("debit_total"))),
                "credit_total": _round_money(_to_number(row.get("credit_total"))),
                "leg_types": [str(item) for item in (row.get("leg_types") or []) if str(item or "").strip()],
            }
            for row in recent_rows
        ],
    }


def _build_aging_buckets(rows: list[dict], key_name: str) -> dict[str, object]:
    bands = {
        "current": 0.0,
        "1_30_days": 0.0,
        "31_60_days": 0.0,
        "61_90_days": 0.0,
        "over_90_days": 0.0,
    }
    detailed: list[dict[str, object]] = []
    today_value = date.today()

    for row in rows:
        amount = _to_number(row.get("due_amount"))
        if amount <= 0:
            continue
        last_activity_raw = str(row.get("last_activity_date") or "").strip()
        age_days = None
        if last_activity_raw:
            try:
                age_days = (today_value - date.fromisoformat(last_activity_raw)).days
            except ValueError:
                age_days = None

        if age_days is None or age_days <= 0:
            bucket = "current"
        elif age_days <= 30:
            bucket = "1_30_days"
        elif age_days <= 60:
            bucket = "31_60_days"
        elif age_days <= 90:
            bucket = "61_90_days"
        else:
            bucket = "over_90_days"

        bands[bucket] += amount
        detailed.append(
            {
                key_name: row.get(key_name),
                "due_amount": _round_money(amount),
                "last_activity_date": last_activity_raw or None,
                "age_days": age_days,
                "bucket": bucket,
            }
        )

    return {
        "bands": {key: _round_money(value) for key, value in bands.items()},
        "top_items": detailed[:20],
    }


def build_business_accounting_context(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    query: str,
    scope: ParsedDateScope | None,
) -> dict[str, object]:
    focus = classify_business_accounting_focus(query)
    start_date, end_date, scope_label = _resolve_statement_scope(scope, focus)
    previous_start, previous_end = _build_previous_window(start_date, end_date)

    snapshot = collect_business_live_snapshot(
        conn,
        user_id=user_id,
        profile_id=profile_id,
    )
    accounts = _fetch_payment_accounts(conn, user_id=user_id, profile_id=profile_id)
    inventory_snapshot = _fetch_inventory_snapshot(conn, user_id=user_id, profile_id=profile_id)
    posting_totals = _build_business_posting_totals(
        conn,
        user_id=user_id,
        profile_id=profile_id,
        start_date=start_date,
        end_date=end_date,
    )
    previous_posting_totals = _build_business_posting_totals(
        conn,
        user_id=user_id,
        profile_id=profile_id,
        start_date=previous_start,
        end_date=previous_end,
    )
    general_ledger_activity = _fetch_general_ledger_activity(
        conn,
        user_id=user_id,
        profile_id=profile_id,
        start_date=start_date,
        end_date=end_date,
    )

    current_positive_accounts = sum(max(0.0, _to_number(row.get("current_balance"))) for row in accounts)
    current_overdrafts = sum(max(0.0, -_to_number(row.get("current_balance"))) for row in accounts)
    receivables = _to_number(snapshot.get("receivable_due_total"))
    payables = _to_number(snapshot.get("payable_due_total"))
    supplier_advances = _fetch_supplier_advance_total(conn, user_id=user_id, profile_id=profile_id)
    inventory_cost = _to_number(inventory_snapshot.get("inventory_value_cost"))

    total_assets = current_positive_accounts + receivables + inventory_cost + supplier_advances
    total_liabilities = payables + current_overdrafts
    closing_equity = total_assets - total_liabilities

    sales_revenue = posting_totals["sales_revenue"]
    other_income = posting_totals["other_income"]
    operating_expenses = posting_totals["operating_expense"]
    net_profit = sales_revenue + other_income - operating_expenses

    # COGS and gross profit — cogs may be zero if no dedicated leg_type exists yet
    cogs = posting_totals["cogs"]
    gross_profit = sales_revenue - cogs
    # If COGS is not posted separately, flag it so the prompt/notes can surface it
    cogs_available = cogs > 0

    previous_net_profit = (
        previous_posting_totals["sales_revenue"]
        + previous_posting_totals["other_income"]
        - previous_posting_totals["operating_expense"]
    )
    previous_sales = previous_posting_totals["sales_revenue"]
    previous_expenses = previous_posting_totals["operating_expense"]
    previous_cogs = previous_posting_totals["cogs"]
    previous_gross_profit = previous_sales - previous_cogs

    cash_movements = _fetch_cash_movements_by_type(
        conn,
        user_id=user_id,
        profile_id=profile_id,
        start_date=start_date,
        end_date=end_date,
    )
    operating_cash_in = 0.0
    operating_cash_out = 0.0
    financing_cash_in = 0.0
    financing_cash_out = 0.0
    investing_cash_in = 0.0
    investing_cash_out = 0.0
    uncategorized_cash = 0.0
    for row in cash_movements:
        txn_type = _normalize_label(row.get("txn_type"))
        net_cash_change = _to_number(row.get("net_cash_change"))
        target_bucket = "uncategorized"
        if txn_type in {"sale", "income", "expense", "receivable_collection", "inventory_in", "stock_in", "purchase"}:
            target_bucket = "operating"
        elif txn_type in {"opening_balance", "capital", "owner_investment"}:
            target_bucket = "financing"
        elif txn_type in {"asset_purchase", "asset_sale"}:
            target_bucket = "investing"

        if target_bucket == "operating":
            if net_cash_change >= 0:
                operating_cash_in += net_cash_change
            else:
                operating_cash_out += abs(net_cash_change)
        elif target_bucket == "financing":
            if net_cash_change >= 0:
                financing_cash_in += net_cash_change
            else:
                financing_cash_out += abs(net_cash_change)
        elif target_bucket == "investing":
            if net_cash_change >= 0:
                investing_cash_in += net_cash_change
            else:
                investing_cash_out += abs(net_cash_change)
        else:
            uncategorized_cash += net_cash_change

    net_cash_change = (
        operating_cash_in
        - operating_cash_out
        + investing_cash_in
        - investing_cash_out
        + financing_cash_in
        - financing_cash_out
        + uncategorized_cash
    )
    closing_cash = current_positive_accounts
    opening_cash = closing_cash - net_cash_change

    current_ratio = _safe_divide(current_positive_accounts + receivables + inventory_cost, payables + current_overdrafts)
    quick_ratio = _safe_divide(current_positive_accounts + receivables, payables + current_overdrafts)
    cash_ratio = _safe_divide(current_positive_accounts, payables + current_overdrafts)
    net_margin = _safe_divide(net_profit, sales_revenue)
    expense_ratio = _safe_divide(operating_expenses, sales_revenue + other_income)

    # Profitability ratios
    gross_margin = _safe_divide(gross_profit, sales_revenue) if cogs_available else None
    ebitda = net_profit  # approximation: no depreciation/amortisation tracked yet
    ebitda_margin = _safe_divide(ebitda, sales_revenue)
    # Return on Equity: net_profit / closing_equity
    roe = _safe_divide(net_profit, closing_equity)
    # Return on Assets: net_profit / total_assets
    roa = _safe_divide(net_profit, total_assets)

    # Leverage / solvency ratios
    debt_to_equity = _safe_divide(total_liabilities, closing_equity)

    # Efficiency ratios
    # AR Turnover: how many times receivables are collected per period
    ar_turnover = _safe_divide(sales_revenue, receivables)
    # Days Sales Outstanding: avg days to collect receivables
    dso = _safe_divide(365.0, ar_turnover) if ar_turnover else None
    # AP Turnover: how many times payables are paid per period (uses purchases if available, else operating_expenses)
    purchases_for_ap = posting_totals["purchases"] if posting_totals["purchases"] > 0 else operating_expenses
    ap_turnover = _safe_divide(purchases_for_ap, payables)
    # Days Payable Outstanding: avg days to pay suppliers
    dpo = _safe_divide(365.0, ap_turnover) if ap_turnover else None
    # Inventory Turnover: COGS / inventory (uses operating_expenses as proxy when COGS not available)
    cogs_for_inv = cogs if cogs_available else operating_expenses
    inventory_turnover = _safe_divide(cogs_for_inv, inventory_cost) if inventory_cost > 0 else None
    # Days Inventory Outstanding: avg days inventory is held
    dio = _safe_divide(365.0, inventory_turnover) if inventory_turnover else None

    customer_aging = _build_aging_buckets(
        list(snapshot.get("customer_due_breakdown") or []),
        "customer_name",
    )
    supplier_aging = _build_aging_buckets(
        list(snapshot.get("supplier_due_breakdown") or []),
        "supplier_name",
    )

    sales_change = sales_revenue - previous_sales
    expense_change = operating_expenses - previous_expenses
    profit_change = net_profit - previous_net_profit

    return {
        "focus": focus,
        "scope": {
            "label": scope_label,
            "start_date": start_date.isoformat() if start_date else None,
            "end_date": end_date.isoformat() if end_date else None,
            "comparison_start_date": previous_start.isoformat() if previous_start else None,
            "comparison_end_date": previous_end.isoformat() if previous_end else None,
            "as_of_date": date.today().isoformat(),
        },
        "balance_sheet": {
            "assets": {
                "cash_and_bank": _round_money(current_positive_accounts),
                # vertical_pct: each asset line as % of total_assets for vertical analysis
                "cash_and_bank_pct": _safe_divide(current_positive_accounts, total_assets),
                "accounts_receivable": _round_money(receivables),
                "accounts_receivable_pct": _safe_divide(receivables, total_assets),
                "inventory": _round_money(inventory_cost),
                "inventory_pct": _safe_divide(inventory_cost, total_assets),
                "supplier_advances": _round_money(supplier_advances),
                "supplier_advances_pct": _safe_divide(supplier_advances, total_assets),
                "total_assets": _round_money(total_assets),
            },
            "liabilities": {
                "accounts_payable": _round_money(payables),
                # vertical_pct: each liability as % of total_assets (standard vertical analysis)
                "accounts_payable_pct": _safe_divide(payables, total_assets),
                "overdrafts": _round_money(current_overdrafts),
                "overdrafts_pct": _safe_divide(current_overdrafts, total_assets),
                "total_liabilities": _round_money(total_liabilities),
                "total_liabilities_pct": _safe_divide(total_liabilities, total_assets),
            },
            "equity": {
                "closing_equity": _round_money(closing_equity),
                "closing_equity_pct": _safe_divide(closing_equity, total_assets),
            },
            "notes": [
                "Vertical % columns show each line as a proportion of total assets (standard common-size balance sheet).",
                "Fixed assets and long-term liabilities are not yet tracked; balance sheet reflects current/working-capital items only.",
                "Equity = Total Assets − Total Liabilities (accounting equation).",
            ],
        },
        "income_statement": {
            "sales_revenue": _round_money(sales_revenue),
            "sales_revenue_pct": 1.0 if sales_revenue > 0 else None,  # base = 100% for vertical analysis
            "cogs": _round_money(cogs) if cogs_available else None,
            "cogs_pct": _safe_divide(cogs, sales_revenue) if cogs_available else None,
            "gross_profit": _round_money(gross_profit) if cogs_available else None,
            "gross_profit_pct": gross_margin,
            "other_income": _round_money(other_income),
            "other_income_pct": _safe_divide(other_income, sales_revenue),
            "operating_expenses": _round_money(operating_expenses),
            "operating_expenses_pct": _safe_divide(operating_expenses, sales_revenue),
            "inventory_added_not_expensed": _round_money(posting_totals["inventory_added"]),
            "ebitda": _round_money(ebitda),
            "ebitda_margin": ebitda_margin,
            "net_profit": _round_money(net_profit),
            "net_profit_pct": net_margin,
            "cogs_available": cogs_available,
            "notes": [
                "Vertical % columns show each line as a proportion of sales revenue (common-size income statement).",
                "COGS is tracked via 'cogs'/'cost_of_goods_sold' leg_types or inventory_asset credits."
                if cogs_available
                else "COGS is not yet posted as a separate ledger leg — gross profit cannot be isolated. Post COGS entries to see gross margin.",
                "EBITDA is approximated as net profit; depreciation and amortisation are not yet tracked.",
                "Inventory purchases (inventory_added) are capitalised as assets, not expensed directly.",
            ],
        },
        "cash_flow_statement": {
            "operating": {
                "cash_in": _round_money(operating_cash_in),
                "cash_out": _round_money(operating_cash_out),
                "net": _round_money(operating_cash_in - operating_cash_out),
            },
            "investing": {
                "cash_in": _round_money(investing_cash_in),
                "cash_out": _round_money(investing_cash_out),
                "net": _round_money(investing_cash_in - investing_cash_out),
            },
            "financing": {
                "cash_in": _round_money(financing_cash_in),
                "cash_out": _round_money(financing_cash_out),
                "net": _round_money(financing_cash_in - financing_cash_out),
            },
            "other_unclassified_net": _round_money(uncategorized_cash),
            "opening_cash": _round_money(opening_cash),
            "closing_cash": _round_money(closing_cash),
            "net_change_in_cash": _round_money(net_cash_change),
        },
        "statement_of_changes_in_equity": {
            "opening_equity": _round_money(closing_equity - net_profit),
            "profit_for_period": _round_money(net_profit),
            "owner_movements_known": None,
            "closing_equity": _round_money(closing_equity),
            "notes": [
                "Owner contribution and drawings are not separately isolated yet unless posted explicitly.",
            ],
        },
        "aging_reports": {
            "accounts_receivable": customer_aging,
            "accounts_payable": supplier_aging,
        },
        "inventory_report": {
            "product_count": int(inventory_snapshot.get("product_count") or 0),
            "total_units": _round_money(_to_number(inventory_snapshot.get("inventory_units_total"))),
            "inventory_value_cost": _round_money(inventory_cost),
        },
        "ratio_analysis": {
            # Liquidity ratios
            "current_ratio": current_ratio,
            "current_ratio_formula": "( Cash + Receivables + Inventory ) ÷ ( Payables + Overdrafts )",
            "quick_ratio": quick_ratio,
            "quick_ratio_formula": "( Cash + Receivables ) ÷ ( Payables + Overdrafts )",
            "cash_ratio": cash_ratio,
            "cash_ratio_formula": "Cash ÷ ( Payables + Overdrafts )",
            # Profitability ratios
            "gross_margin": gross_margin,
            "gross_margin_formula": "Gross Profit ÷ Sales Revenue",
            "net_margin": net_margin,
            "net_margin_formula": "Net Profit ÷ Sales Revenue",
            "ebitda_margin": ebitda_margin,
            "ebitda_margin_formula": "EBITDA ÷ Sales Revenue  (EBITDA approximated as Net Profit)",
            "return_on_equity": roe,
            "return_on_equity_formula": "Net Profit ÷ Closing Equity",
            "return_on_assets": roa,
            "return_on_assets_formula": "Net Profit ÷ Total Assets",
            "expense_ratio": expense_ratio,
            "expense_ratio_formula": "Operating Expenses ÷ Total Revenue",
            # Leverage / solvency ratios
            "debt_to_equity": debt_to_equity,
            "debt_to_equity_formula": "Total Liabilities ÷ Closing Equity",
            # Efficiency ratios
            "ar_turnover": ar_turnover,
            "ar_turnover_formula": "Sales Revenue ÷ Accounts Receivable",
            "days_sales_outstanding": _safe_divide(round(dso, 1), 1) if dso else None,
            "days_sales_outstanding_formula": "365 ÷ AR Turnover",
            "ap_turnover": ap_turnover,
            "ap_turnover_formula": "Purchases (or Operating Expenses) ÷ Accounts Payable",
            "days_payable_outstanding": _safe_divide(round(dpo, 1), 1) if dpo else None,
            "days_payable_outstanding_formula": "365 ÷ AP Turnover",
            "inventory_turnover": inventory_turnover,
            "inventory_turnover_formula": "COGS (or Operating Expenses) ÷ Inventory Value",
            "days_inventory_outstanding": _safe_divide(round(dio, 1), 1) if dio else None,
            "days_inventory_outstanding_formula": "365 ÷ Inventory Turnover",
            "notes": [
                "Ratios marked with * use proxies: AP Turnover uses operating expenses when purchase postings are absent; Inventory Turnover uses operating expenses when COGS is not posted.",
                "Interest Coverage Ratio requires interest expense postings, which are not yet tracked.",
                "All ratios are point-in-time using closing balances, not period averages.",
            ],
        },
        "trend_analysis": {
            "sales": {
                "current": _round_money(sales_revenue),
                "previous": _round_money(previous_sales),
                "change": _round_money(sales_change),
                "change_percent": _safe_divide(sales_change, previous_sales),
                "vertical_percent_of_revenue": 1.0 if sales_revenue > 0 else None,
            },
            "gross_profit": {
                "current": _round_money(gross_profit) if cogs_available else None,
                "previous": _round_money(previous_gross_profit) if cogs_available else None,
                "change": _round_money(gross_profit - previous_gross_profit) if cogs_available else None,
                "change_percent": _safe_divide(gross_profit - previous_gross_profit, previous_gross_profit) if cogs_available else None,
                "vertical_percent_of_revenue": gross_margin,
            },
            "operating_expenses": {
                "current": _round_money(operating_expenses),
                "previous": _round_money(previous_expenses),
                "change": _round_money(expense_change),
                "change_percent": _safe_divide(expense_change, previous_expenses),
                "vertical_percent_of_revenue": _safe_divide(operating_expenses, sales_revenue or 0),
            },
            "net_profit": {
                "current": _round_money(net_profit),
                "previous": _round_money(previous_net_profit),
                "change": _round_money(profit_change),
                "change_percent": _safe_divide(profit_change, previous_net_profit),
                "vertical_percent_of_revenue": _safe_divide(net_profit, sales_revenue or 0),
            },
            "notes": [
                "Horizontal analysis: 'change_percent' shows period-over-period growth rate.",
                "Vertical analysis: 'vertical_percent_of_revenue' shows each line as % of sales revenue.",
                "Previous period is the equivalent prior window (e.g. last month vs the month before).",
            ],
        },
        "payment_accounts": [
            {
                "id": str(row.get("id") or ""),
                "name": str(row.get("name") or "Account"),
                "type": str(row.get("type") or ""),
                "current_balance": _round_money(_to_number(row.get("current_balance"))),
                "overdraft_limit": _round_money(_to_number(row.get("overdraft_limit"))),
            }
            for row in accounts
        ],
        "general_ledger": general_ledger_activity,
        "live_snapshot": snapshot,
    }
