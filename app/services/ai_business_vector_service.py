import hashlib
import json
import re
from collections import defaultdict
from datetime import date, datetime, timezone

import httpx
from psycopg import Connection
from psycopg import Error as PsycopgError
from psycopg.pq import TransactionStatus

from app.core.config import Settings
from app.core.db import apply_db_auth_context

_VECTOR_DOCS_RELATIONS = ["business.vector_documents", "business.ai_documents"]
_VECTOR_JOBS_RELATIONS = ["business.vector_jobs", "business.ai_index_jobs"]
_VECTOR_MATCH_FUNCTIONS = ["business.match_vector_documents", "business.match_ai_documents"]

_PHASE1_SOURCE_KINDS = {
    "financial_overview",
    "customer_due",
    "supplier_due",
    "customer_purchase_total",
    "supplier_purchase_total",
    "stock_product",
    "stock_summary",
    "invoice_outstanding",
}

_SOURCE_KIND_ALIAS: dict[str, set[str]] = {
    "financial_overview": {"financial_overview", "summary", "account"},
    "customer_due": {"customer_due"},
    "supplier_due": {"supplier_due"},
    "customer_purchase_total": {"customer_purchase_total"},
    "supplier_purchase_total": {"supplier_purchase_total"},
    "stock_product": {"stock_product", "inventory_low_stock"},
    "stock_summary": {"stock_summary", "inventory"},
    "invoice_outstanding": {"invoice_outstanding", "invoice_due"},
}


def _reset_failed_transaction(conn: Connection) -> None:
    try:
        if conn.info.transaction_status == TransactionStatus.INERROR:
            conn.rollback()
    except Exception:
        pass



def _first_existing_relation(conn: Connection, candidates: list[str]) -> str | None:
    for relation in candidates:
        try:
            with conn.cursor() as cur:
                cur.execute("select to_regclass(%(relation)s) as rel", {"relation": relation})
                row = cur.fetchone() or {}
            if row.get("rel"):
                return relation
        except PsycopgError:
            _reset_failed_transaction(conn)
            continue
    return None



def _first_existing_function(conn: Connection, candidates: list[str]) -> str | None:
    for candidate in candidates:
        if "." not in candidate:
            continue
        schema_name, func_name = candidate.split(".", 1)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select exists (
                      select 1
                      from pg_proc p
                      join pg_namespace n on n.oid = p.pronamespace
                      where n.nspname = %(schema_name)s
                        and p.proname = %(func_name)s
                    ) as exists_fn
                    """,
                    {"schema_name": schema_name, "func_name": func_name},
                )
                row = cur.fetchone() or {}
            if bool(row.get("exists_fn")):
                return candidate
        except PsycopgError:
            _reset_failed_transaction(conn)
            continue
    return None



def _relation_has_column(conn: Connection, relation: str, column_name: str) -> bool:
    if "." not in relation:
        return False
    schema_name, table_name = relation.split(".", 1)
    try:
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
    except PsycopgError:
        _reset_failed_transaction(conn)
        return False
    return bool(row.get("has_col"))



def _to_vector_literal(embedding: list[float]) -> str:
    normalized = [str(round(float(item), 8)) for item in embedding]
    return f"[{','.join(normalized)}]"



def _is_placeholder(value: str | None) -> bool:
    if not value:
        return True
    normalized = value.strip().lower()
    if not normalized:
        return True
    return (
        normalized.startswith("your_")
        or "example.com" in normalized
        or "your-backend" in normalized
    )



def _embedding_endpoint(settings: Settings) -> str | None:
    if settings.embedding_api_endpoint and str(settings.embedding_api_endpoint).strip():
        return str(settings.embedding_api_endpoint).strip()
    if settings.supabase_url:
        return f"{settings.supabase_url.rstrip('/')}/functions/v1/embed"
    return None



def create_embedding(
    *,
    text: str,
    settings: Settings,
) -> list[float] | None:
    endpoint = _embedding_endpoint(settings)
    if _is_placeholder(endpoint):
        return None

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if settings.embedding_api_key and not _is_placeholder(settings.embedding_api_key):
        header_name = settings.embedding_api_key_header or "x-api-key"
        header_value = settings.embedding_api_key
        if header_name.lower() == "authorization" and not header_value.lower().startswith("bearer "):
            header_value = f"Bearer {header_value}"
        headers[header_name] = header_value

    is_supabase_fn = endpoint and ".supabase.co/functions/v1/" in endpoint
    if is_supabase_fn and settings.supabase_anon_key:
        headers["apikey"] = settings.supabase_anon_key
        headers.setdefault("Authorization", f"Bearer {settings.supabase_anon_key}")

    payload = {
        "text": text,
        "modelId": settings.embedding_model_id,
    }

    try:
        # Keep embedding timeout lower than model generation timeout to avoid
        # spending the full chat budget on semantic retrieval.
        timeout = httpx.Timeout(min(float(settings.ai_timeout_sec), 10.0))
        with httpx.Client(timeout=timeout) as client:
            response = client.post(endpoint, headers=headers, json=payload)
    except httpx.HTTPError:
        return None

    if response.status_code >= 400:
        return None

    try:
        parsed = response.json()
    except ValueError:
        return None

    candidates = [
        parsed,
        parsed.get("embedding") if isinstance(parsed, dict) else None,
        parsed.get("vector") if isinstance(parsed, dict) else None,
        (parsed.get("data") or {}).get("embedding")
        if isinstance(parsed, dict) and isinstance(parsed.get("data"), dict)
        else None,
        (parsed.get("data") or {}).get("vector")
        if isinstance(parsed, dict) and isinstance(parsed.get("data"), dict)
        else None,
        (parsed.get("result") or {}).get("embedding")
        if isinstance(parsed, dict) and isinstance(parsed.get("result"), dict)
        else None,
        (parsed.get("result") or {}).get("vector")
        if isinstance(parsed, dict) and isinstance(parsed.get("result"), dict)
        else None,
    ]

    embedding: list[float] | None = None
    for candidate in candidates:
        if isinstance(candidate, list) and candidate:
            try:
                vector = [float(item) for item in candidate]
            except (TypeError, ValueError):
                continue
            if all(item == item and item != float("inf") and item != float("-inf") for item in vector):
                embedding = vector
                break

    if not embedding:
        return None

    expected = int(settings.embedding_dim)
    if expected > 0 and len(embedding) != expected:
        return None
    return embedding



def _sanitize_text(value: object, fallback: str = "") -> str:
    text = str(value or "").strip()
    return text if text else fallback



def _slugify(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower())
    normalized = normalized.strip("-")
    return normalized or "unknown"



def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()



def _compute_content_hash(content: str, metadata: dict, schema_version: str) -> str:
    canonical = json.dumps(
        {
            "content": content,
            "metadata": metadata,
            "schema_version": schema_version,
        },
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()



def _doc_key(source_kind: str, source_id: str, chunk_index: int) -> tuple[str, str, int]:
    return (source_kind, source_id, chunk_index)



def enqueue_business_ai_refresh_job(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    source_kind: str = "full_refresh",
    source_id: str = "*",
) -> None:
    jobs_relation = _first_existing_relation(conn, _VECTOR_JOBS_RELATIONS)
    if not jobs_relation:
        return

    has_next_attempt_at = _relation_has_column(conn, jobs_relation, "next_attempt_at")
    with conn.cursor() as cur:
        if has_next_attempt_at:
            try:
                cur.execute(
                    f"""
                    insert into {jobs_relation} (
                      user_id, profile_id, source_kind, source_id, status, attempts, last_error, next_attempt_at, created_at, updated_at
                    ) values (
                      %(user_id)s::uuid,
                      %(profile_id)s::uuid,
                      %(source_kind)s::text,
                      %(source_id)s::text,
                      'pending',
                      0,
                      null,
                      now(),
                      now(),
                      now()
                    )
                    on conflict (user_id, profile_id, source_kind, source_id)
                    where status in ('pending', 'running')
                    do update set
                      status = 'pending',
                      last_error = null,
                      next_attempt_at = now(),
                      updated_at = now()
                    """,
                    {
                        "user_id": user_id,
                        "profile_id": profile_id,
                        "source_kind": source_kind,
                        "source_id": source_id,
                    },
                )
                return
            except PsycopgError:
                _reset_failed_transaction(conn)

        cur.execute(
            f"""
            insert into {jobs_relation} (
              user_id, profile_id, source_kind, source_id, status, attempts, created_at, updated_at
            ) values (
              %(user_id)s::uuid,
              %(profile_id)s::uuid,
              %(source_kind)s::text,
              %(source_id)s::text,
              'pending',
              0,
              now(),
              now()
            )
            """,
            {
                "user_id": user_id,
                "profile_id": profile_id,
                "source_kind": source_kind,
                "source_id": source_id,
            },
        )



def _build_financial_overview_docs(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    postings_relation: str | None,
    entries_relation: str | None,
    as_of: str,
) -> list[dict]:
    if not postings_relation:
        return []

    with conn.cursor() as cur:
        cur.execute(
            f"""
            select
              coalesce(sum(case when leg_type in ('sales_revenue','income_bucket') and direction='credit' then amount else 0 end), 0) as total_income,
              coalesce(sum(case when leg_type in ('cogs','expense_bucket') and direction='debit' then amount else 0 end), 0) as total_expense,
              coalesce(sum(case when leg_type='receivable' and direction='debit' then amount when leg_type='receivable' and direction='credit' then -amount else 0 end), 0) as total_receivable_due,
              coalesce(sum(case when leg_type='payable' and direction='credit' then amount when leg_type='payable' and direction='debit' then -amount else 0 end), 0) as total_payable_due
            from {postings_relation}
            where user_id = %(user_id)s::uuid
              and profile_id = %(profile_id)s::uuid
            """,
            {"user_id": user_id, "profile_id": profile_id},
        )
        totals = cur.fetchone() or {}

        month_income = 0.0
        month_expense = 0.0
        if entries_relation:
            cur.execute(
                f"""
                select
                  coalesce(sum(case when lp.leg_type in ('sales_revenue','income_bucket') and lp.direction='credit' then lp.amount else 0 end), 0) as month_income,
                  coalesce(sum(case when lp.leg_type in ('cogs','expense_bucket') and lp.direction='debit' then lp.amount else 0 end), 0) as month_expense
                from {postings_relation} lp
                join {entries_relation} le on le.id = lp.entry_id
                where lp.user_id = %(user_id)s::uuid
                  and lp.profile_id = %(profile_id)s::uuid
                  and le.date >= date_trunc('month', current_date)::date
                  and le.date <= current_date
                """,
                {"user_id": user_id, "profile_id": profile_id},
            )
            month_row = cur.fetchone() or {}
            month_income = float(month_row.get("month_income") or 0)
            month_expense = float(month_row.get("month_expense") or 0)

    total_income = float(totals.get("total_income") or 0)
    total_expense = float(totals.get("total_expense") or 0)
    total_receivable_due = float(totals.get("total_receivable_due") or 0)
    total_payable_due = float(totals.get("total_payable_due") or 0)
    net_total = total_income - total_expense

    return [
        {
            "source_kind": "financial_overview",
            "source_id": "overview:global",
            "chunk_index": 0,
            "schema_version": "v2",
            "as_of": as_of,
            "content": (
                "Business financial overview. "
                f"Total income NPR {total_income:.2f}. "
                f"Total expense NPR {total_expense:.2f}. "
                f"Net NPR {net_total:.2f}. "
                f"Receivable due NPR {total_receivable_due:.2f}. "
                f"Payable due NPR {total_payable_due:.2f}. "
                f"This month income NPR {month_income:.2f}. "
                f"This month expense NPR {month_expense:.2f}."
            ),
            "metadata": {
                "as_of": as_of,
                "income_total": total_income,
                "expense_total": total_expense,
                "net_total": net_total,
                "receivable_due_total": total_receivable_due,
                "payable_due_total": total_payable_due,
                "income_this_month": month_income,
                "expense_this_month": month_expense,
                "authoritative_numeric": False,
            },
        }
    ]



def _build_customer_due_docs(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    postings_relation: str | None,
    customers_relation: str | None,
    entries_relation: str | None,
    as_of: str,
) -> tuple[list[dict], dict[str, float]]:
    if not postings_relation or not customers_relation:
        return [], {}

    join_entries = ""
    last_activity_expr = "null::text as last_activity_date"
    if entries_relation:
        join_entries = f"left join {entries_relation} le on le.id = lp.entry_id"
        last_activity_expr = "max(le.date)::text as last_activity_date"

    with conn.cursor() as cur:
        cur.execute(
            f"""
            select
              c.id::text as customer_id,
              c.name as customer_name,
              coalesce(
                sum(
                  case
                    when lp.direction='debit' then lp.amount
                    when lp.direction='credit' then -lp.amount
                    else 0
                  end
                ),
                0
              ) as due_amount,
              {last_activity_expr}
            from {customers_relation} c
            left join {postings_relation} lp
              on lp.user_id = c.user_id
             and lp.profile_id = c.profile_id
             and lp.leg_type = 'receivable'
             and lp.ref_id = c.id
            {join_entries}
            where c.user_id = %(user_id)s::uuid
              and c.profile_id = %(profile_id)s::uuid
              and c.is_active = true
            group by c.id, c.name
            having coalesce(
                sum(
                  case
                    when lp.direction='debit' then lp.amount
                    when lp.direction='credit' then -lp.amount
                    else 0
                  end
                ),
                0
              ) > 0
            order by due_amount desc
            limit 50
            """,
            {"user_id": user_id, "profile_id": profile_id},
        )
        rows = cur.fetchall() or []

    docs: list[dict] = []
    due_map: dict[str, float] = {}
    for row in rows:
        customer_id = _sanitize_text(row.get("customer_id"))
        customer_name = _sanitize_text(row.get("customer_name"), "Customer")
        due_amount = float(row.get("due_amount") or 0)
        if customer_id:
            due_map[customer_id] = due_amount
        docs.append(
            {
                "source_kind": "customer_due",
                "source_id": f"customer:{customer_id or _slugify(customer_name)}:due",
                "chunk_index": 0,
                "schema_version": "v2",
                "as_of": as_of,
                "content": (
                    f"Customer receivable summary. {customer_name} has pending receivable NPR {due_amount:.2f}."
                ),
                "metadata": {
                    "as_of": as_of,
                    "customer_id": customer_id or None,
                    "customer_name": customer_name,
                    "due_amount": due_amount,
                    "last_activity_date": _sanitize_text(row.get("last_activity_date")) or None,
                    "authoritative_numeric": False,
                },
            }
        )
    return docs, due_map



def _build_supplier_due_docs(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    postings_relation: str | None,
    suppliers_relation: str | None,
    entries_relation: str | None,
    as_of: str,
) -> tuple[list[dict], dict[str, float]]:
    if not postings_relation or not suppliers_relation:
        return [], {}

    join_entries = ""
    last_activity_expr = "null::text as last_activity_date"
    if entries_relation:
        join_entries = f"left join {entries_relation} le on le.id = lp.entry_id"
        last_activity_expr = "max(le.date)::text as last_activity_date"

    with conn.cursor() as cur:
        cur.execute(
            f"""
            select
              s.id::text as supplier_id,
              s.name as supplier_name,
              coalesce(
                sum(
                  case
                    when lp.direction='credit' then lp.amount
                    when lp.direction='debit' then -lp.amount
                    else 0
                  end
                ),
                0
              ) as due_amount,
              {last_activity_expr}
            from {suppliers_relation} s
            left join {postings_relation} lp
              on lp.user_id = s.user_id
             and lp.profile_id = s.profile_id
             and lp.leg_type = 'payable'
             and lp.ref_id = s.id
            {join_entries}
            where s.user_id = %(user_id)s::uuid
              and s.profile_id = %(profile_id)s::uuid
              and s.is_active = true
            group by s.id, s.name
            having coalesce(
                sum(
                  case
                    when lp.direction='credit' then lp.amount
                    when lp.direction='debit' then -lp.amount
                    else 0
                  end
                ),
                0
              ) > 0
            order by due_amount desc
            limit 50
            """,
            {"user_id": user_id, "profile_id": profile_id},
        )
        rows = cur.fetchall() or []

    docs: list[dict] = []
    due_map: dict[str, float] = {}
    for row in rows:
        supplier_id = _sanitize_text(row.get("supplier_id"))
        supplier_name = _sanitize_text(row.get("supplier_name"), "Supplier")
        due_amount = float(row.get("due_amount") or 0)
        if supplier_id:
            due_map[supplier_id] = due_amount
        docs.append(
            {
                "source_kind": "supplier_due",
                "source_id": f"supplier:{supplier_id or _slugify(supplier_name)}:due",
                "chunk_index": 0,
                "schema_version": "v2",
                "as_of": as_of,
                "content": (
                    f"Supplier payable summary. {supplier_name} has pending payable NPR {due_amount:.2f}."
                ),
                "metadata": {
                    "as_of": as_of,
                    "supplier_id": supplier_id or None,
                    "supplier_name": supplier_name,
                    "due_amount": due_amount,
                    "last_activity_date": _sanitize_text(row.get("last_activity_date")) or None,
                    "authoritative_numeric": False,
                },
            }
        )

    return docs, due_map



def _build_customer_purchase_total_docs(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    invoices_relation: str | None,
    invoice_payments_relation: str | None,
    customers_relation: str | None,
    as_of: str,
) -> list[dict]:
    if not invoices_relation:
        return []

    has_due_amount = _relation_has_column(conn, invoices_relation, "due_amount")
    has_paid_amount = _relation_has_column(conn, invoices_relation, "paid_amount")
    has_customer_name_snapshot = _relation_has_column(conn, invoices_relation, "customer_name_snapshot")
    has_customer_id = _relation_has_column(conn, invoices_relation, "customer_id")

    join_payments_sql = ""
    paid_amount_expr = "0::numeric"
    if has_paid_amount:
        paid_amount_expr = "coalesce(i.paid_amount, 0)"
    elif invoice_payments_relation:
        join_payments_sql = f"""
            left join (
              select invoice_id, coalesce(sum(amount), 0) as paid_amount
              from {invoice_payments_relation}
              where user_id = %(user_id)s::uuid
                and profile_id = %(profile_id)s::uuid
              group by invoice_id
            ) pay on pay.invoice_id = i.id
        """
        paid_amount_expr = "coalesce(pay.paid_amount, 0)"

    if has_due_amount:
        due_amount_expr = (
            f"coalesce(i.due_amount, greatest(coalesce(i.total, 0) - ({paid_amount_expr}), 0))"
        )
    else:
        due_amount_expr = f"greatest(coalesce(i.total, 0) - ({paid_amount_expr}), 0)"

    customer_name_expr = "'Customer'"
    customer_id_expr = "null::text"
    join_customer_sql = ""
    if has_customer_id:
        customer_id_expr = "i.customer_id::text"
        if customers_relation:
            join_customer_sql = f"""
                left join {customers_relation} c
                  on c.id = i.customer_id
                 and c.user_id = i.user_id
                 and c.profile_id = i.profile_id
            """
            customer_name_expr = "coalesce(c.name, 'Customer')"
        elif has_customer_name_snapshot:
            customer_name_expr = "coalesce(nullif(i.customer_name_snapshot, ''), 'Customer')"
    elif has_customer_name_snapshot:
        customer_name_expr = "coalesce(nullif(i.customer_name_snapshot, ''), 'Customer')"

    with conn.cursor() as cur:
        cur.execute(
            f"""
            select
              {customer_id_expr} as customer_id,
              {customer_name_expr} as customer_name,
              count(*)::int as invoice_count,
              coalesce(sum(coalesce(i.total, 0)), 0) as total_purchase,
              coalesce(sum({paid_amount_expr}), 0) as paid_total,
              coalesce(sum({due_amount_expr}), 0) as due_total,
              max(i.date)::text as last_invoice_date
            from {invoices_relation} i
            {join_payments_sql}
            {join_customer_sql}
            where i.user_id = %(user_id)s::uuid
              and i.profile_id = %(profile_id)s::uuid
            group by 1, 2
            having coalesce(sum(coalesce(i.total, 0)), 0) > 0
            order by total_purchase desc
            limit 50
            """,
            {"user_id": user_id, "profile_id": profile_id},
        )
        rows = cur.fetchall() or []

    docs: list[dict] = []
    for row in rows:
        customer_id = _sanitize_text(row.get("customer_id"))
        customer_name = _sanitize_text(row.get("customer_name"), "Customer")
        source_identity = customer_id or f"name-{_slugify(customer_name)}"
        total_purchase = float(row.get("total_purchase") or 0)
        paid_total = float(row.get("paid_total") or 0)
        due_total = float(row.get("due_total") or 0)
        invoice_count = int(row.get("invoice_count") or 0)

        docs.append(
            {
                "source_kind": "customer_purchase_total",
                "source_id": f"customer:{source_identity}:purchase_total",
                "chunk_index": 0,
                "schema_version": "v2",
                "as_of": as_of,
                "content": (
                    f"Customer purchase summary. {customer_name} has {invoice_count} invoice(s). "
                    f"Total purchase NPR {total_purchase:.2f}, paid NPR {paid_total:.2f}, due NPR {due_total:.2f}."
                ),
                "metadata": {
                    "as_of": as_of,
                    "customer_id": customer_id or None,
                    "customer_name": customer_name,
                    "invoice_count": invoice_count,
                    "total_purchase": total_purchase,
                    "paid_total": paid_total,
                    "due_total": due_total,
                    "last_invoice_date": _sanitize_text(row.get("last_invoice_date")) or None,
                    "authoritative_numeric": False,
                },
            }
        )

    return docs



def _build_supplier_purchase_total_docs(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    entries_relation: str | None,
    suppliers_relation: str | None,
    supplier_due_map: dict[str, float],
    as_of: str,
) -> list[dict]:
    if not entries_relation:
        return []

    has_metadata = _relation_has_column(conn, entries_relation, "metadata")
    has_txn_type = _relation_has_column(conn, entries_relation, "txn_type")
    if not has_metadata or not has_txn_type:
        return []

    join_supplier_sql = ""
    supplier_name_expr = "coalesce(nullif(le.metadata->>'supplier_name', ''), 'Supplier')"
    if suppliers_relation:
        join_supplier_sql = f"""
            left join {suppliers_relation} s
              on s.id::text = nullif(le.metadata->>'supplier_id', '')
             and s.user_id = le.user_id
             and s.profile_id = le.profile_id
        """
        supplier_name_expr = "coalesce(nullif(le.metadata->>'supplier_name', ''), s.name, 'Supplier')"

    with conn.cursor() as cur:
        cur.execute(
            f"""
            select
              nullif(le.metadata->>'supplier_id', '') as supplier_id,
              {supplier_name_expr} as supplier_name,
              count(*)::int as purchase_entry_count,
              coalesce(sum(coalesce(le.amount, 0)), 0) as purchase_total,
              max(le.date)::text as last_purchase_date
            from {entries_relation} le
            {join_supplier_sql}
            where le.user_id = %(user_id)s::uuid
              and le.profile_id = %(profile_id)s::uuid
              and le.txn_type in ('inventory_in', 'stock_in', 'purchase')
              and (
                nullif(le.metadata->>'supplier_id', '') is not null
                or nullif(le.metadata->>'supplier_name', '') is not null
              )
            group by 1, 2
            having coalesce(sum(coalesce(le.amount, 0)), 0) > 0
            order by purchase_total desc
            limit 50
            """,
            {"user_id": user_id, "profile_id": profile_id},
        )
        rows = cur.fetchall() or []

    docs: list[dict] = []
    for row in rows:
        supplier_id = _sanitize_text(row.get("supplier_id"))
        supplier_name = _sanitize_text(row.get("supplier_name"), "Supplier")
        source_identity = supplier_id or f"name-{_slugify(supplier_name)}"
        purchase_total = float(row.get("purchase_total") or 0)
        purchase_entry_count = int(row.get("purchase_entry_count") or 0)
        payable_due = float(supplier_due_map.get(supplier_id, 0)) if supplier_id else 0.0

        docs.append(
            {
                "source_kind": "supplier_purchase_total",
                "source_id": f"supplier:{source_identity}:purchase_total",
                "chunk_index": 0,
                "schema_version": "v2",
                "as_of": as_of,
                "content": (
                    f"Supplier purchase summary. {supplier_name} has {purchase_entry_count} stock purchase entry(ies). "
                    f"Total purchase NPR {purchase_total:.2f}. Current payable due NPR {payable_due:.2f}."
                ),
                "metadata": {
                    "as_of": as_of,
                    "supplier_id": supplier_id or None,
                    "supplier_name": supplier_name,
                    "purchase_total": purchase_total,
                    "purchase_entry_count": purchase_entry_count,
                    "current_payable_due": payable_due,
                    "last_purchase_date": _sanitize_text(row.get("last_purchase_date")) or None,
                    "authoritative_numeric": False,
                },
            }
        )

    return docs



def _build_stock_docs(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    products_relation: str | None,
    as_of: str,
) -> list[dict]:
    if not products_relation:
        return []

    stock_state_relation = _first_existing_relation(conn, ["business.product_stock_state", "public.product_stock_state"])
    categories_relation = _first_existing_relation(conn, ["business.product_categories", "public.product_categories"])

    has_selling_price = _relation_has_column(conn, products_relation, "selling_price")
    has_price = _relation_has_column(conn, products_relation, "price")
    has_quantity = _relation_has_column(conn, products_relation, "quantity")
    has_stock_qty = bool(stock_state_relation and _relation_has_column(conn, stock_state_relation, "qty_on_hand"))
    has_avg_unit_cost = bool(stock_state_relation and _relation_has_column(conn, stock_state_relation, "avg_unit_cost"))

    qty_expr = "0::numeric"
    if has_stock_qty and has_quantity:
        qty_expr = "coalesce(pss.qty_on_hand, p.quantity, 0)"
    elif has_stock_qty:
        qty_expr = "coalesce(pss.qty_on_hand, 0)"
    elif has_quantity:
        qty_expr = "coalesce(p.quantity, 0)"

    avg_unit_cost_expr = "0::numeric"
    if has_avg_unit_cost and has_price:
        avg_unit_cost_expr = "coalesce(pss.avg_unit_cost, p.price, 0)"
    elif has_avg_unit_cost:
        avg_unit_cost_expr = "coalesce(pss.avg_unit_cost, 0)"
    elif has_price:
        avg_unit_cost_expr = "coalesce(p.price, 0)"

    selling_price_expr = "0::numeric"
    if has_selling_price and has_price:
        selling_price_expr = "coalesce(p.selling_price, p.price, 0)"
    elif has_selling_price:
        selling_price_expr = "coalesce(p.selling_price, 0)"
    elif has_price:
        selling_price_expr = "coalesce(p.price, 0)"

    join_stock_state = f"left join {stock_state_relation} pss on pss.product_id = p.id" if stock_state_relation else ""
    join_categories = (
        f"left join {categories_relation} pc on pc.id = p.category_id and pc.profile_id = p.profile_id"
        if categories_relation and _relation_has_column(conn, products_relation, "category_id")
        else ""
    )
    category_expr = "coalesce(pc.name, 'Uncategorized')" if join_categories else "'Uncategorized'"

    with conn.cursor() as cur:
        cur.execute(
            f"""
            select
              p.id::text as product_id,
              p.name as product_name,
              {category_expr} as category_name,
              {qty_expr} as qty_on_hand,
              {avg_unit_cost_expr} as avg_unit_cost,
              {selling_price_expr} as selling_price
            from {products_relation} p
            {join_stock_state}
            {join_categories}
            where p.user_id = %(user_id)s::uuid
              and p.profile_id = %(profile_id)s::uuid
              and p.is_active = true
            order by p.name asc
            limit 500
            """,
            {"user_id": user_id, "profile_id": profile_id},
        )
        rows = cur.fetchall() or []

    docs: list[dict] = []
    total_units = 0.0
    stock_value_cost_total = 0.0
    stock_value_selling_total = 0.0
    low_stock_count = 0

    for row in rows:
        product_id = _sanitize_text(row.get("product_id"))
        product_name = _sanitize_text(row.get("product_name"), "Product")
        category_name = _sanitize_text(row.get("category_name"), "Uncategorized")
        qty_on_hand = float(row.get("qty_on_hand") or 0)
        avg_unit_cost = float(row.get("avg_unit_cost") or 0)
        selling_price = float(row.get("selling_price") or 0)
        stock_value_cost = qty_on_hand * avg_unit_cost
        stock_value_selling = qty_on_hand * selling_price
        low_stock_flag = qty_on_hand <= 5

        total_units += qty_on_hand
        stock_value_cost_total += stock_value_cost
        stock_value_selling_total += stock_value_selling
        if low_stock_flag:
            low_stock_count += 1

        docs.append(
            {
                "source_kind": "stock_product",
                "source_id": f"product:{product_id or _slugify(product_name)}",
                "chunk_index": 0,
                "schema_version": "v2",
                "as_of": as_of,
                "content": (
                    f"Product stock summary. {product_name} in category {category_name}. "
                    f"Quantity on hand {qty_on_hand:.3f}. Avg unit cost NPR {avg_unit_cost:.2f}. "
                    f"Selling price NPR {selling_price:.2f}. Low stock {'yes' if low_stock_flag else 'no'}."
                ),
                "metadata": {
                    "as_of": as_of,
                    "product_id": product_id or None,
                    "product_name": product_name,
                    "category_name": category_name,
                    "qty_on_hand": qty_on_hand,
                    "avg_unit_cost": avg_unit_cost,
                    "selling_price": selling_price,
                    "stock_value_cost": stock_value_cost,
                    "stock_value_selling": stock_value_selling,
                    "low_stock_flag": low_stock_flag,
                    "authoritative_numeric": False,
                },
            }
        )

    docs.append(
        {
            "source_kind": "stock_summary",
            "source_id": "stock:summary",
            "chunk_index": 0,
            "schema_version": "v2",
            "as_of": as_of,
            "content": (
                "Inventory summary. "
                f"Active products {len(rows)}. "
                f"Total units {total_units:.3f}. "
                f"Stock value at cost NPR {stock_value_cost_total:.2f}. "
                f"Stock value at selling NPR {stock_value_selling_total:.2f}. "
                f"Low stock products {low_stock_count}."
            ),
            "metadata": {
                "as_of": as_of,
                "total_products": len(rows),
                "total_units": total_units,
                "stock_value_cost_total": stock_value_cost_total,
                "stock_value_selling_total": stock_value_selling_total,
                "low_stock_count": low_stock_count,
                "authoritative_numeric": False,
            },
        }
    )

    return docs



def _build_invoice_outstanding_docs(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    invoices_relation: str | None,
    invoice_payments_relation: str | None,
    customers_relation: str | None,
    as_of: str,
) -> list[dict]:
    if not invoices_relation:
        return []

    has_due_amount = _relation_has_column(conn, invoices_relation, "due_amount")
    has_paid_amount = _relation_has_column(conn, invoices_relation, "paid_amount")
    has_customer_name_snapshot = _relation_has_column(conn, invoices_relation, "customer_name_snapshot")
    has_customer_id = _relation_has_column(conn, invoices_relation, "customer_id")
    has_created_at = _relation_has_column(conn, invoices_relation, "created_at")

    join_payments_sql = ""
    paid_amount_expr = "0::numeric"
    if has_paid_amount:
        paid_amount_expr = "coalesce(i.paid_amount, 0)"
    elif invoice_payments_relation:
        join_payments_sql = f"""
            left join (
              select invoice_id, coalesce(sum(amount), 0) as paid_amount
              from {invoice_payments_relation}
              where user_id = %(user_id)s::uuid
                and profile_id = %(profile_id)s::uuid
              group by invoice_id
            ) pay on pay.invoice_id = i.id
        """
        paid_amount_expr = "coalesce(pay.paid_amount, 0)"

    if has_due_amount:
        due_amount_expr = (
            f"coalesce(i.due_amount, greatest(coalesce(i.total, 0) - ({paid_amount_expr}), 0))"
        )
    else:
        due_amount_expr = f"greatest(coalesce(i.total, 0) - ({paid_amount_expr}), 0)"

    join_customer_sql = ""
    customer_name_expr = "'Customer'"
    if has_customer_name_snapshot:
        customer_name_expr = "coalesce(nullif(i.customer_name_snapshot, ''), 'Customer')"
    elif has_customer_id and customers_relation:
        join_customer_sql = f"""
            left join {customers_relation} c
              on c.id = i.customer_id
             and c.user_id = i.user_id
             and c.profile_id = i.profile_id
        """
        customer_name_expr = "coalesce(c.name, 'Customer')"

    order_by_sql = "i.date desc, i.created_at desc" if has_created_at else "i.date desc"

    with conn.cursor() as cur:
        cur.execute(
            f"""
            select
              i.id::text as invoice_id,
              i.invoice_no,
              i.date,
              i.payment_status,
              coalesce(i.total, 0) as total,
              {paid_amount_expr} as paid_amount,
              {due_amount_expr} as due_amount,
              {customer_name_expr} as customer_name
            from {invoices_relation} i
            {join_payments_sql}
            {join_customer_sql}
            where i.user_id = %(user_id)s::uuid
              and i.profile_id = %(profile_id)s::uuid
              and i.payment_status in ('partial', 'due')
            order by {order_by_sql}
            limit 50
            """,
            {"user_id": user_id, "profile_id": profile_id},
        )
        rows = cur.fetchall() or []

    docs: list[dict] = []
    for row in rows:
        invoice_id = _sanitize_text(row.get("invoice_id"), "invoice")
        invoice_no = _sanitize_text(row.get("invoice_no"), "")
        customer_name = _sanitize_text(row.get("customer_name"), "Customer")
        total = float(row.get("total") or 0)
        paid_amount = float(row.get("paid_amount") or 0)
        due_amount = float(row.get("due_amount") or 0)

        docs.append(
            {
                "source_kind": "invoice_outstanding",
                "source_id": f"invoice:{invoice_id}:outstanding",
                "chunk_index": 0,
                "schema_version": "v2",
                "as_of": as_of,
                "content": (
                    f"Outstanding invoice summary. Invoice {invoice_no or invoice_id} for {customer_name}. "
                    f"Total NPR {total:.2f}, paid NPR {paid_amount:.2f}, due NPR {due_amount:.2f}, "
                    f"status {_sanitize_text(row.get('payment_status'), 'due')}"
                ),
                "metadata": {
                    "as_of": as_of,
                    "invoice_id": invoice_id,
                    "invoice_no": invoice_no,
                    "customer_name": customer_name,
                    "total": total,
                    "paid": paid_amount,
                    "due": due_amount,
                    "status": _sanitize_text(row.get("payment_status"), "due"),
                    "date": _sanitize_text(row.get("date")) or None,
                    "authoritative_numeric": False,
                },
            }
        )

    return docs



def _collect_business_documents(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
) -> list[dict]:
    postings_relation = _first_existing_relation(conn, ["business.ledger_postings", "public.ledger_postings"])
    entries_relation = _first_existing_relation(conn, ["business.ledger_entries", "public.ledger_entries"])
    customers_relation = _first_existing_relation(conn, ["business.customers", "public.customers"])
    suppliers_relation = _first_existing_relation(conn, ["business.suppliers", "public.suppliers"])
    invoices_relation = _first_existing_relation(conn, ["business.invoices", "public.invoices"])
    invoice_payments_relation = _first_existing_relation(
        conn, ["business.invoice_payments", "public.invoice_payments"]
    )
    products_relation = _first_existing_relation(conn, ["business.products", "public.products"])

    as_of = _now_iso()
    docs: list[dict] = []

    docs.extend(
        _build_financial_overview_docs(
            conn,
            user_id=user_id,
            profile_id=profile_id,
            postings_relation=postings_relation,
            entries_relation=entries_relation,
            as_of=as_of,
        )
    )

    customer_due_docs, _ = _build_customer_due_docs(
        conn,
        user_id=user_id,
        profile_id=profile_id,
        postings_relation=postings_relation,
        customers_relation=customers_relation,
        entries_relation=entries_relation,
        as_of=as_of,
    )
    docs.extend(customer_due_docs)

    docs.extend(
        _build_customer_purchase_total_docs(
            conn,
            user_id=user_id,
            profile_id=profile_id,
            invoices_relation=invoices_relation,
            invoice_payments_relation=invoice_payments_relation,
            customers_relation=customers_relation,
            as_of=as_of,
        )
    )

    supplier_due_docs, supplier_due_map = _build_supplier_due_docs(
        conn,
        user_id=user_id,
        profile_id=profile_id,
        postings_relation=postings_relation,
        suppliers_relation=suppliers_relation,
        entries_relation=entries_relation,
        as_of=as_of,
    )
    docs.extend(supplier_due_docs)

    docs.extend(
        _build_supplier_purchase_total_docs(
            conn,
            user_id=user_id,
            profile_id=profile_id,
            entries_relation=entries_relation,
            suppliers_relation=suppliers_relation,
            supplier_due_map=supplier_due_map,
            as_of=as_of,
        )
    )

    docs.extend(
        _build_stock_docs(
            conn,
            user_id=user_id,
            profile_id=profile_id,
            products_relation=products_relation,
            as_of=as_of,
        )
    )

    docs.extend(
        _build_invoice_outstanding_docs(
            conn,
            user_id=user_id,
            profile_id=profile_id,
            invoices_relation=invoices_relation,
            invoice_payments_relation=invoice_payments_relation,
            customers_relation=customers_relation,
            as_of=as_of,
        )
    )

    return docs



def _upsert_business_documents_incremental(
    conn: Connection,
    *,
    settings: Settings,
    user_id: str,
    profile_id: str,
    docs: list[dict],
) -> dict[str, int]:
    docs_relation = _first_existing_relation(conn, _VECTOR_DOCS_RELATIONS)
    if not docs_relation:
        return {"total_docs": 0, "changed_docs": 0, "embedded_docs": 0, "tombstoned_docs": 0}

    has_content_hash = _relation_has_column(conn, docs_relation, "content_hash")
    has_indexed_at = _relation_has_column(conn, docs_relation, "indexed_at")
    has_schema_version = _relation_has_column(conn, docs_relation, "schema_version")
    has_embed_model = _relation_has_column(conn, docs_relation, "embed_model")
    has_embed_version = _relation_has_column(conn, docs_relation, "embed_version")
    has_as_of = _relation_has_column(conn, docs_relation, "as_of")
    has_is_tombstone = _relation_has_column(conn, docs_relation, "is_tombstone")
    has_deleted_at = _relation_has_column(conn, docs_relation, "deleted_at")

    normalized_docs: list[dict] = []
    for raw in docs:
        source_kind = _sanitize_text(raw.get("source_kind"), "financial_overview")
        source_id = _sanitize_text(raw.get("source_id"), "document")
        chunk_index = int(raw.get("chunk_index") or 0)
        content = _sanitize_text(raw.get("content"))
        if not content:
            continue
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        schema_version = _sanitize_text(raw.get("schema_version"), "v2")
        as_of = _sanitize_text(raw.get("as_of") or metadata.get("as_of")) or None
        content_hash = _compute_content_hash(content, metadata, schema_version)
        normalized_docs.append(
            {
                "source_kind": source_kind,
                "source_id": source_id,
                "chunk_index": chunk_index,
                "content": content,
                "metadata": metadata,
                "schema_version": schema_version,
                "as_of": as_of,
                "content_hash": content_hash,
            }
        )

    existing_by_key: dict[tuple[str, str, int], dict] = {}
    with conn.cursor() as cur:
        cur.execute(
            f"""
            select
              source_kind,
              source_id,
              chunk_index,
              {"content_hash" if has_content_hash else "null::text"} as content_hash,
              {"is_tombstone" if has_is_tombstone else "false"} as is_tombstone,
              content,
              metadata,
              {"schema_version" if has_schema_version else "'v1'::text"} as schema_version
            from {docs_relation}
            where user_id = %(user_id)s::uuid
              and profile_id = %(profile_id)s::uuid
            """,
            {"user_id": user_id, "profile_id": profile_id},
        )
        for row in cur.fetchall() or []:
            key = _doc_key(
                _sanitize_text(row.get("source_kind")),
                _sanitize_text(row.get("source_id")),
                int(row.get("chunk_index") or 0),
            )
            existing_by_key[key] = row

    changed_docs: list[dict] = []
    current_keys: set[tuple[str, str, int]] = set()
    for doc in normalized_docs:
        key = _doc_key(doc["source_kind"], doc["source_id"], doc["chunk_index"])
        current_keys.add(key)
        existing = existing_by_key.get(key)
        if not existing:
            changed_docs.append(doc)
            continue

        existing_hash = _sanitize_text(existing.get("content_hash"))
        if not existing_hash:
            existing_hash = _compute_content_hash(
                _sanitize_text(existing.get("content")),
                existing.get("metadata") if isinstance(existing.get("metadata"), dict) else {},
                _sanitize_text(existing.get("schema_version"), "v1"),
            )

        if existing_hash != doc["content_hash"] or bool(existing.get("is_tombstone")):
            changed_docs.append(doc)

    embedded_docs = 0
    for doc in changed_docs:
        embedding = create_embedding(text=doc["content"], settings=settings)
        doc["embedding"] = embedding
        if embedding:
            embedded_docs += 1

    insert_columns = [
        "user_id",
        "profile_id",
        "source_kind",
        "source_id",
        "chunk_index",
        "content",
        "metadata",
        "embedding",
        "updated_at",
    ]
    insert_values = [
        "%(user_id)s::uuid",
        "%(profile_id)s::uuid",
        "%(source_kind)s::text",
        "%(source_id)s::text",
        "%(chunk_index)s::int",
        "%(content)s::text",
        "%(metadata)s::jsonb",
        "%(embedding)s::extensions.vector",
        "now()",
    ]
    update_set = [
        "content = excluded.content",
        "metadata = excluded.metadata",
        "embedding = excluded.embedding",
        "updated_at = now()",
    ]

    if has_content_hash:
        insert_columns.append("content_hash")
        insert_values.append("%(content_hash)s::text")
        update_set.append("content_hash = excluded.content_hash")
    if has_indexed_at:
        insert_columns.append("indexed_at")
        insert_values.append("now()")
        update_set.append("indexed_at = now()")
    if has_schema_version:
        insert_columns.append("schema_version")
        insert_values.append("%(schema_version)s::text")
        update_set.append("schema_version = excluded.schema_version")
    if has_embed_model:
        insert_columns.append("embed_model")
        insert_values.append("%(embed_model)s::text")
        update_set.append("embed_model = excluded.embed_model")
    if has_embed_version:
        insert_columns.append("embed_version")
        insert_values.append("%(embed_version)s::text")
        update_set.append("embed_version = excluded.embed_version")
    if has_as_of:
        insert_columns.append("as_of")
        insert_values.append("%(as_of)s::timestamptz")
        update_set.append("as_of = excluded.as_of")
    if has_is_tombstone:
        insert_columns.append("is_tombstone")
        insert_values.append("false")
        update_set.append("is_tombstone = false")
    if has_deleted_at:
        insert_columns.append("deleted_at")
        insert_values.append("null")
        update_set.append("deleted_at = null")

    upsert_sql = f"""
        insert into {docs_relation} ({', '.join(insert_columns)})
        values ({', '.join(insert_values)})
        on conflict (user_id, profile_id, source_kind, source_id, chunk_index)
        do update set {', '.join(update_set)}
    """

    with conn.transaction():
        with conn.cursor() as cur:
            for doc in changed_docs:
                embedding_value = doc.get("embedding")
                cur.execute(
                    upsert_sql,
                    {
                        "user_id": user_id,
                        "profile_id": profile_id,
                        "source_kind": doc["source_kind"],
                        "source_id": doc["source_id"],
                        "chunk_index": doc["chunk_index"],
                        "content": doc["content"],
                        "metadata": json.dumps(doc["metadata"]),
                        "embedding": _to_vector_literal(embedding_value)
                        if isinstance(embedding_value, list)
                        else None,
                        "content_hash": doc["content_hash"],
                        "schema_version": doc["schema_version"],
                        "embed_model": settings.embedding_model_id if embedding_value else None,
                        "embed_version": "v1" if embedding_value else None,
                        "as_of": doc["as_of"],
                    },
                )

            stale_keys = [key for key in existing_by_key.keys() if key not in current_keys]
            for source_kind, source_id, chunk_index in stale_keys:
                if has_is_tombstone:
                    tombstone_set_parts = [
                        "is_tombstone = true",
                        "embedding = null",
                        "updated_at = now()",
                    ]
                    if has_deleted_at:
                        tombstone_set_parts.append("deleted_at = now()")
                    if has_indexed_at:
                        tombstone_set_parts.append("indexed_at = now()")
                    cur.execute(
                        f"""
                        update {docs_relation}
                        set {', '.join(tombstone_set_parts)}
                        where user_id = %(user_id)s::uuid
                          and profile_id = %(profile_id)s::uuid
                          and source_kind = %(source_kind)s::text
                          and source_id = %(source_id)s::text
                          and chunk_index = %(chunk_index)s::int
                        """,
                        {
                            "user_id": user_id,
                            "profile_id": profile_id,
                            "source_kind": source_kind,
                            "source_id": source_id,
                            "chunk_index": chunk_index,
                        },
                    )
                else:
                    cur.execute(
                        f"""
                        delete from {docs_relation}
                        where user_id = %(user_id)s::uuid
                          and profile_id = %(profile_id)s::uuid
                          and source_kind = %(source_kind)s::text
                          and source_id = %(source_id)s::text
                          and chunk_index = %(chunk_index)s::int
                        """,
                        {
                            "user_id": user_id,
                            "profile_id": profile_id,
                            "source_kind": source_kind,
                            "source_id": source_id,
                            "chunk_index": chunk_index,
                        },
                    )

    return {
        "total_docs": len(normalized_docs),
        "changed_docs": len(changed_docs),
        "embedded_docs": embedded_docs,
        "tombstoned_docs": len([key for key in existing_by_key.keys() if key not in current_keys]),
    }



def _run_full_profile_refresh(
    conn: Connection,
    *,
    settings: Settings,
    user_id: str,
    profile_id: str,
) -> dict[str, int]:
    docs = _collect_business_documents(conn, user_id=user_id, profile_id=profile_id)
    return _upsert_business_documents_incremental(
        conn,
        settings=settings,
        user_id=user_id,
        profile_id=profile_id,
        docs=docs,
    )



def process_pending_business_vector_jobs(
    conn: Connection,
    *,
    settings: Settings,
    user_id: str,
    profile_id: str,
    max_jobs: int = 2,
    max_attempts: int = 5,
) -> list[str]:
    warnings: list[str] = []
    apply_db_auth_context(conn, user_id)
    jobs_relation = _first_existing_relation(conn, _VECTOR_JOBS_RELATIONS)
    if not jobs_relation:
        return warnings

    has_next_attempt_at = _relation_has_column(conn, jobs_relation, "next_attempt_at")
    safe_limit = max(1, min(int(max_jobs or 2), 10))

    pending_filter = "and status = 'pending'"
    next_attempt_filter = "and next_attempt_at <= now()" if has_next_attempt_at else ""

    jobs: list[dict] = []
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                f"""
                select id, source_kind, source_id, attempts
                from {jobs_relation}
                where user_id = %(user_id)s::uuid
                  and profile_id = %(profile_id)s::uuid
                  {pending_filter}
                  {next_attempt_filter}
                order by created_at asc
                limit %(limit)s
                for update skip locked
                """,
                {"user_id": user_id, "profile_id": profile_id, "limit": safe_limit},
            )
            jobs = cur.fetchall() or []
            if not jobs:
                return warnings

            job_ids = [int(row.get("id")) for row in jobs if row.get("id") is not None]
            cur.execute(
                f"""
                update {jobs_relation}
                set status = 'running',
                    attempts = attempts + 1,
                    updated_at = now()
                where id = any(%(job_ids)s::bigint[])
                """,
                {"job_ids": job_ids},
            )

    for job in jobs:
        job_id = int(job.get("id") or 0)
        attempts_after_running = int(job.get("attempts") or 0) + 1
        try:
            stats = _run_full_profile_refresh(
                conn,
                settings=settings,
                user_id=user_id,
                profile_id=profile_id,
            )
            with conn.transaction():
                with conn.cursor() as cur:
                    if has_next_attempt_at:
                        cur.execute(
                            f"""
                            update {jobs_relation}
                            set status = 'done',
                                last_error = null,
                                next_attempt_at = now(),
                                updated_at = now()
                            where id = %(job_id)s::bigint
                            """,
                            {"job_id": job_id},
                        )
                    else:
                        cur.execute(
                            f"""
                            update {jobs_relation}
                            set status = 'done',
                                last_error = null,
                                updated_at = now()
                            where id = %(job_id)s::bigint
                            """,
                            {"job_id": job_id},
                        )
            if stats.get("changed_docs", 0) > 0 and stats.get("embedded_docs", 0) == 0:
                warnings.append("Business vector embeddings unavailable; lexical fallback will be used.")
        except Exception as exc:  # pragma: no cover - defensive runtime safety
            is_terminal = attempts_after_running >= max_attempts
            backoff_seconds = min(3600, 15 * (2 ** max(0, attempts_after_running - 1)))
            with conn.transaction():
                with conn.cursor() as cur:
                    if has_next_attempt_at:
                        cur.execute(
                            f"""
                            update {jobs_relation}
                            set status = %(status)s,
                                last_error = %(last_error)s,
                                next_attempt_at = now() + (%(backoff_seconds)s || ' seconds')::interval,
                                updated_at = now()
                            where id = %(job_id)s::bigint
                            """,
                            {
                                "status": "failed" if is_terminal else "pending",
                                "last_error": str(exc),
                                "backoff_seconds": backoff_seconds,
                                "job_id": job_id,
                            },
                        )
                    else:
                        cur.execute(
                            f"""
                            update {jobs_relation}
                            set status = %(status)s,
                                last_error = %(last_error)s,
                                updated_at = now()
                            where id = %(job_id)s::bigint
                            """,
                            {
                                "status": "failed" if is_terminal else "pending",
                                "last_error": str(exc),
                                "job_id": job_id,
                            },
                        )
            warnings.append("Business vector indexing job failed; retry scheduled.")

    return warnings



def process_due_business_vector_jobs(
    conn: Connection,
    *,
    settings: Settings,
    max_profiles: int = 5,
    max_jobs_per_profile: int = 2,
) -> list[str]:
    """
    Process pending business vector jobs across profile scopes.
    Intended for background worker usage (non-chat path).
    """
    warnings: list[str] = []
    jobs_relation = _first_existing_relation(conn, _VECTOR_JOBS_RELATIONS)
    if not jobs_relation:
        return warnings

    has_next_attempt_at = _relation_has_column(conn, jobs_relation, "next_attempt_at")
    next_attempt_filter = "and next_attempt_at <= now()" if has_next_attempt_at else ""
    safe_profiles = max(1, min(int(max_profiles or 5), 50))
    safe_jobs_per_profile = max(1, min(int(max_jobs_per_profile or 2), 5))

    profile_rows: list[dict] = []
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    select
                      user_id::text as user_id,
                      profile_id::text as profile_id,
                      min(created_at) as first_pending_at
                    from {jobs_relation}
                    where status = 'pending'
                      {next_attempt_filter}
                    group by user_id, profile_id
                    order by first_pending_at asc
                    limit %(limit)s::int
                    """,
                    {"limit": safe_profiles},
                )
                profile_rows = cur.fetchall() or []
    except PsycopgError:
        _reset_failed_transaction(conn)
        return warnings

    for row in profile_rows:
        user_id = _sanitize_text(row.get("user_id"))
        profile_id = _sanitize_text(row.get("profile_id"))
        if not user_id or not profile_id:
            continue
        warnings.extend(
            process_pending_business_vector_jobs(
                conn,
                settings=settings,
                user_id=user_id,
                profile_id=profile_id,
                max_jobs=safe_jobs_per_profile,
            )
        )

    return warnings



def _count_business_docs(conn: Connection, *, user_id: str, profile_id: str) -> int:
    docs_relation = _first_existing_relation(conn, _VECTOR_DOCS_RELATIONS)
    if not docs_relation:
        return 0
    has_is_tombstone = _relation_has_column(conn, docs_relation, "is_tombstone")
    tombstone_filter = "and is_tombstone = false" if has_is_tombstone else ""

    with conn.cursor() as cur:
        cur.execute(
            f"""
            select count(*) as total
            from {docs_relation}
            where user_id = %(user_id)s::uuid
              and profile_id = %(profile_id)s::uuid
              {tombstone_filter}
            """,
            {"user_id": user_id, "profile_id": profile_id},
        )
        row = cur.fetchone() or {}
    return int(row.get("total") or 0)



def _count_pending_jobs(conn: Connection, *, user_id: str, profile_id: str) -> int:
    jobs_relation = _first_existing_relation(conn, _VECTOR_JOBS_RELATIONS)
    if not jobs_relation:
        return 0
    has_next_attempt_at = _relation_has_column(conn, jobs_relation, "next_attempt_at")
    next_attempt_filter = "and next_attempt_at <= now()" if has_next_attempt_at else ""

    with conn.cursor() as cur:
        cur.execute(
            f"""
            select count(*) as total
            from {jobs_relation}
            where user_id = %(user_id)s::uuid
              and profile_id = %(profile_id)s::uuid
              and status = 'pending'
              {next_attempt_filter}
            """,
            {"user_id": user_id, "profile_id": profile_id},
        )
        row = cur.fetchone() or {}
    return int(row.get("total") or 0)



def get_business_vector_status_warnings(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
) -> list[str]:
    warnings: list[str] = []
    docs_relation = _first_existing_relation(conn, _VECTOR_DOCS_RELATIONS)
    if not docs_relation:
        return ["Business vector store is unavailable. Apply the latest Supabase migrations."]

    docs_total = _count_business_docs(conn, user_id=user_id, profile_id=profile_id)
    if docs_total == 0:
        warnings.append("Business semantic index has no documents yet. Answers will use SQL snapshot first.")

    pending_jobs = _count_pending_jobs(conn, user_id=user_id, profile_id=profile_id)
    if pending_jobs > 0:
        warnings.append("Business semantic index update is pending. Context may lag behind recent writes.")

    return warnings



def refresh_business_vectors_if_needed(
    conn: Connection,
    *,
    settings: Settings,
    user_id: str,
    profile_id: str,
) -> list[str]:
    """
    Backward-compat function name.
    v2 behavior intentionally avoids chat-time indexing and returns status warnings only.
    """
    _ = settings
    return get_business_vector_status_warnings(conn, user_id=user_id, profile_id=profile_id)



def _source_kind_family(source_id: str) -> str:
    parts = (source_id or "").split(":")
    if len(parts) >= 2 and parts[0] in {"customer", "supplier", "product", "invoice"}:
        return f"{parts[0]}:{parts[1]}"
    return source_id



def _infer_source_kinds_for_query(query: str) -> set[str]:
    text = (query or "").strip().lower()
    if not text:
        return set(_PHASE1_SOURCE_KINDS)

    selected: set[str] = {"financial_overview"}

    if any(token in text for token in ["due", "receivable", "collect", "customer due", "outstanding customer"]):
        selected.update({"customer_due", "customer_purchase_total", "invoice_outstanding"})

    if any(token in text for token in ["payable", "supplier due", "supplier payable", "vendor", "purchase due"]):
        selected.update({"supplier_due", "supplier_purchase_total"})

    if any(token in text for token in ["customer", "client", "invoice", "sales", "sold"]):
        selected.update({"customer_due", "customer_purchase_total", "invoice_outstanding"})

    if any(token in text for token in ["supplier", "vendor", "purchase", "stock in"]):
        selected.update({"supplier_due", "supplier_purchase_total", "stock_product"})

    if any(token in text for token in ["stock", "inventory", "product", "low stock", "qty", "quantity"]):
        selected.update({"stock_product", "stock_summary"})

    if any(token in text for token in ["overview", "summary", "income", "expense", "profit", "loss", "net"]):
        selected.update({"financial_overview"})

    if not selected:
        selected = set(_PHASE1_SOURCE_KINDS)

    return selected



def _expand_source_kind_aliases(source_kinds: set[str]) -> list[str]:
    expanded: set[str] = set()
    for source_kind in source_kinds:
        expanded.update(_SOURCE_KIND_ALIAS.get(source_kind, {source_kind}))
    return sorted(expanded)



def _fetch_vector_candidates(
    conn: Connection,
    *,
    settings: Settings,
    user_id: str,
    profile_id: str,
    query: str,
    source_kinds: list[str],
    threshold: float,
    limit: int,
) -> list[dict]:
    docs_relation = _first_existing_relation(conn, _VECTOR_DOCS_RELATIONS)
    if not docs_relation:
        return []

    query_embedding = create_embedding(text=query, settings=settings)
    if not query_embedding:
        return []

    has_is_tombstone = _relation_has_column(conn, docs_relation, "is_tombstone")
    has_indexed_at = _relation_has_column(conn, docs_relation, "indexed_at")
    recency_expr = "coalesce(indexed_at, updated_at, created_at)" if has_indexed_at else "coalesce(updated_at, created_at)"
    tombstone_filter = "and d.is_tombstone = false" if has_is_tombstone else ""
    source_filter = "and d.source_kind = any(%(source_kinds)s::text[])" if source_kinds else ""

    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    select
                      d.source_kind,
                      d.source_id,
                      d.chunk_index,
                      d.content,
                      d.metadata,
                      1 - (d.embedding OPERATOR(extensions.<=>) %(query_embedding)s::extensions.vector) as similarity,
                      extract(epoch from {recency_expr}) as recency_epoch
                    from {docs_relation} d
                    where d.user_id = %(user_id)s::uuid
                      and d.profile_id = %(profile_id)s::uuid
                      {tombstone_filter}
                      {source_filter}
                      and d.embedding is not null
                      and 1 - (d.embedding OPERATOR(extensions.<=>) %(query_embedding)s::extensions.vector) >= %(threshold)s::float
                    order by
                      d.embedding OPERATOR(extensions.<=>) %(query_embedding)s::extensions.vector,
                      {recency_expr} desc
                    limit %(limit)s::int
                    """,
                    {
                        "user_id": user_id,
                        "profile_id": profile_id,
                        "source_kinds": source_kinds,
                        "query_embedding": _to_vector_literal(query_embedding),
                        "threshold": threshold,
                        "limit": limit,
                    },
                )
                return cur.fetchall() or []
    except PsycopgError:
        _reset_failed_transaction(conn)

    match_function = _first_existing_function(conn, _VECTOR_MATCH_FUNCTIONS)
    if not match_function:
        return []

    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    select source_kind, source_id, chunk_index, content, metadata, similarity
                    from {match_function}(
                      %(user_id)s::uuid,
                      %(profile_id)s::uuid,
                      %(query_embedding)s::extensions.vector,
                      %(threshold)s::float,
                      %(limit)s::int
                    )
                    """,
                    {
                        "user_id": user_id,
                        "profile_id": profile_id,
                        "query_embedding": _to_vector_literal(query_embedding),
                        "threshold": threshold,
                        "limit": limit,
                    },
                )
                rows = cur.fetchall() or []
    except PsycopgError:
        _reset_failed_transaction(conn)
        return []

    if not source_kinds:
        return rows
    source_kind_set = set(source_kinds)
    return [row for row in rows if _sanitize_text(row.get("source_kind")) in source_kind_set]



def _fetch_lexical_candidates(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    query: str,
    source_kinds: list[str],
    limit: int,
) -> list[dict]:
    docs_relation = _first_existing_relation(conn, _VECTOR_DOCS_RELATIONS)
    if not docs_relation:
        return []

    has_is_tombstone = _relation_has_column(conn, docs_relation, "is_tombstone")
    has_indexed_at = _relation_has_column(conn, docs_relation, "indexed_at")

    tombstone_filter = "and d.is_tombstone = false" if has_is_tombstone else ""
    source_filter = "and d.source_kind = any(%(source_kinds)s::text[])" if source_kinds else ""
    recency_expr = "coalesce(d.indexed_at, d.updated_at, d.created_at)" if has_indexed_at else "coalesce(d.updated_at, d.created_at)"

    query_like = f"%{query}%"

    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    select
                      d.source_kind,
                      d.source_id,
                      d.chunk_index,
                      d.content,
                      d.metadata,
                      greatest(
                        ts_rank_cd(
                          to_tsvector('simple', coalesce(d.content, '')),
                          websearch_to_tsquery('simple', %(query)s)
                        ),
                        0
                      ) as similarity,
                      extract(epoch from {recency_expr}) as recency_epoch
                    from {docs_relation} d
                    where d.user_id = %(user_id)s::uuid
                      and d.profile_id = %(profile_id)s::uuid
                      {tombstone_filter}
                      {source_filter}
                      and (
                        to_tsvector('simple', coalesce(d.content, '')) @@ websearch_to_tsquery('simple', %(query)s)
                        or d.content ilike %(query_like)s
                      )
                    order by similarity desc, {recency_expr} desc
                    limit %(limit)s::int
                    """,
                    {
                        "user_id": user_id,
                        "profile_id": profile_id,
                        "source_kinds": source_kinds,
                        "query": query,
                        "query_like": query_like,
                        "limit": limit,
                    },
                )
                return cur.fetchall() or []
    except PsycopgError:
        _reset_failed_transaction(conn)

    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    select
                      d.source_kind,
                      d.source_id,
                      d.chunk_index,
                      d.content,
                      d.metadata,
                      0::double precision as similarity,
                      extract(epoch from {recency_expr}) as recency_epoch
                    from {docs_relation} d
                    where d.user_id = %(user_id)s::uuid
                      and d.profile_id = %(profile_id)s::uuid
                      {tombstone_filter}
                      {source_filter}
                      and d.content ilike %(query_like)s
                    order by {recency_expr} desc
                    limit %(limit)s::int
                    """,
                    {
                        "user_id": user_id,
                        "profile_id": profile_id,
                        "source_kinds": source_kinds,
                        "query_like": query_like,
                        "limit": limit,
                    },
                )
                return cur.fetchall() or []
    except PsycopgError:
        _reset_failed_transaction(conn)
        return []



def _fuse_candidates(
    *,
    vector_rows: list[dict],
    lexical_rows: list[dict],
    final_limit: int,
) -> list[dict]:
    if final_limit <= 0:
        return []

    by_key: dict[tuple[str, str, int], dict] = {}
    scores: dict[tuple[str, str, int], float] = defaultdict(float)
    kind_counts: dict[str, int] = defaultdict(int)
    family_counts: dict[str, int] = defaultdict(int)

    now_epoch = datetime.now(timezone.utc).timestamp()

    def _merge_row(row: dict, base_rank: int, channel: str) -> None:
        key = _doc_key(
            _sanitize_text(row.get("source_kind")),
            _sanitize_text(row.get("source_id")),
            int(row.get("chunk_index") or 0),
        )
        if key not in by_key:
            by_key[key] = {
                "source_kind": key[0],
                "source_id": key[1],
                "chunk_index": key[2],
                "content": _sanitize_text(row.get("content")),
                "metadata": row.get("metadata") if isinstance(row.get("metadata"), dict) else {},
                "similarity": float(row.get("similarity") or 0),
            }
        else:
            by_key[key]["similarity"] = max(
                float(by_key[key].get("similarity") or 0),
                float(row.get("similarity") or 0),
            )

        rrf_score = 1.0 / (60.0 + float(base_rank))
        similarity_bonus = max(0.0, min(float(row.get("similarity") or 0), 1.0))
        if channel == "vector":
            rrf_score += 0.08 * similarity_bonus
        else:
            rrf_score += 0.05 * similarity_bonus

        recency_epoch = float(row.get("recency_epoch") or 0)
        if recency_epoch > 0:
            age_days = max(0.0, (now_epoch - recency_epoch) / 86400.0)
            recency_bonus = 0.02 * (1.0 / (1.0 + age_days))
            rrf_score += recency_bonus

        scores[key] += rrf_score

    for index, row in enumerate(vector_rows, start=1):
        _merge_row(row, index, "vector")

    for index, row in enumerate(lexical_rows, start=1):
        _merge_row(row, index, "lexical")

    ranked_keys = sorted(scores.keys(), key=lambda key: scores[key], reverse=True)

    selected: list[dict] = []
    deferred: list[dict] = []
    for key in ranked_keys:
        row = by_key[key]
        source_kind = _sanitize_text(row.get("source_kind"))
        source_family = _source_kind_family(_sanitize_text(row.get("source_id")))

        if kind_counts[source_kind] >= 2:
            continue

        if source_family and family_counts[source_family] >= 1:
            deferred.append(row)
            continue

        selected.append(row)
        kind_counts[source_kind] += 1
        if source_family:
            family_counts[source_family] += 1
        if len(selected) >= final_limit:
            return selected

    for row in deferred:
        source_kind = _sanitize_text(row.get("source_kind"))
        if kind_counts[source_kind] >= 2:
            continue
        selected.append(row)
        kind_counts[source_kind] += 1
        if len(selected) >= final_limit:
            return selected

    return selected



def retrieve_business_vector_matches(
    conn: Connection,
    *,
    settings: Settings,
    user_id: str,
    profile_id: str,
    query: str,
    match_threshold: float = 0.55,
    match_count: int = 8,
) -> list[dict]:
    docs_relation = _first_existing_relation(conn, _VECTOR_DOCS_RELATIONS)
    if not docs_relation:
        return []

    normalized_query = (query or "").strip()
    if not normalized_query:
        return []

    safe_threshold = max(0.0, min(float(match_threshold), 1.0))
    safe_final_count = max(1, min(int(match_count), 20))
    vector_candidate_limit = max(24, safe_final_count * 3)
    lexical_candidate_limit = max(24, safe_final_count * 3)

    intent_source_kinds = _infer_source_kinds_for_query(normalized_query)
    allowed_source_kinds = _expand_source_kind_aliases(intent_source_kinds)

    vector_rows = _fetch_vector_candidates(
        conn,
        settings=settings,
        user_id=user_id,
        profile_id=profile_id,
        query=normalized_query,
        source_kinds=allowed_source_kinds,
        threshold=safe_threshold,
        limit=vector_candidate_limit,
    )

    lexical_rows = _fetch_lexical_candidates(
        conn,
        user_id=user_id,
        profile_id=profile_id,
        query=normalized_query,
        source_kinds=allowed_source_kinds,
        limit=lexical_candidate_limit,
    )

    return _fuse_candidates(
        vector_rows=vector_rows,
        lexical_rows=lexical_rows,
        final_limit=safe_final_count,
    )



def collect_business_live_snapshot(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
) -> dict:
    postings_relation = _first_existing_relation(conn, ["business.ledger_postings", "public.ledger_postings"])
    entries_relation = _first_existing_relation(conn, ["business.ledger_entries", "public.ledger_entries"])
    invoices_relation = _first_existing_relation(conn, ["business.invoices", "public.invoices"])
    invoice_payments_relation = _first_existing_relation(
        conn, ["business.invoice_payments", "public.invoice_payments"]
    )
    products_relation = _first_existing_relation(conn, ["business.products", "public.products"])
    customers_relation = _first_existing_relation(conn, ["business.customers", "public.customers"])
    suppliers_relation = _first_existing_relation(conn, ["business.suppliers", "public.suppliers"])

    snapshot: dict[str, object] = {
        "today": date.today().isoformat(),
        "income_total": 0.0,
        "expense_total": 0.0,
        "net_total": 0.0,
        "income_this_month": 0.0,
        "expense_this_month": 0.0,
        "receivable_due_total": 0.0,
        "payable_due_total": 0.0,
        "outstanding_invoice_count": 0,
        "outstanding_invoice_due_total": 0.0,
        "stock_product_count": 0,
        "stock_units_total": 0.0,
        "customer_due_count": 0,
        "supplier_due_count": 0,
        "customer_due_breakdown": [],
        "supplier_due_breakdown": [],
        "customer_due_by_name": {},
        "supplier_due_by_name": {},
    }

    if postings_relation:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                select
                  coalesce(sum(case when leg_type in ('sales_revenue','income_bucket') and direction='credit' then amount else 0 end), 0) as income_total,
                  coalesce(sum(case when leg_type in ('cogs','expense_bucket') and direction='debit' then amount else 0 end), 0) as expense_total,
                  coalesce(sum(case when leg_type='receivable' and direction='debit' then amount when leg_type='receivable' and direction='credit' then -amount else 0 end), 0) as receivable_due_total,
                  coalesce(sum(case when leg_type='payable' and direction='credit' then amount when leg_type='payable' and direction='debit' then -amount else 0 end), 0) as payable_due_total
                from {postings_relation}
                where user_id = %(user_id)s::uuid
                  and profile_id = %(profile_id)s::uuid
                """,
                {"user_id": user_id, "profile_id": profile_id},
            )
            totals = cur.fetchone() or {}
            snapshot["income_total"] = float(totals.get("income_total") or 0)
            snapshot["expense_total"] = float(totals.get("expense_total") or 0)
            snapshot["net_total"] = float(snapshot["income_total"]) - float(snapshot["expense_total"])
            snapshot["receivable_due_total"] = float(totals.get("receivable_due_total") or 0)
            snapshot["payable_due_total"] = float(totals.get("payable_due_total") or 0)

            if entries_relation:
                cur.execute(
                    f"""
                    select
                      coalesce(sum(case when lp.leg_type in ('sales_revenue','income_bucket') and lp.direction='credit' then lp.amount else 0 end), 0) as income_month,
                      coalesce(sum(case when lp.leg_type in ('cogs','expense_bucket') and lp.direction='debit' then lp.amount else 0 end), 0) as expense_month
                    from {postings_relation} lp
                    join {entries_relation} le on le.id = lp.entry_id
                    where lp.user_id = %(user_id)s::uuid
                      and lp.profile_id = %(profile_id)s::uuid
                      and le.date >= date_trunc('month', current_date)::date
                      and le.date <= current_date
                    """,
                    {"user_id": user_id, "profile_id": profile_id},
                )
                month = cur.fetchone() or {}
                snapshot["income_this_month"] = float(month.get("income_month") or 0)
                snapshot["expense_this_month"] = float(month.get("expense_month") or 0)

    if invoices_relation:
        has_due_amount = _relation_has_column(conn, invoices_relation, "due_amount")
        has_paid_amount = _relation_has_column(conn, invoices_relation, "paid_amount")

        join_payments_sql = ""
        paid_amount_expr = "0::numeric"
        if has_paid_amount:
            paid_amount_expr = "coalesce(i.paid_amount, 0)"
        elif invoice_payments_relation:
            join_payments_sql = f"""
                left join (
                  select invoice_id, coalesce(sum(amount), 0) as paid_amount
                  from {invoice_payments_relation}
                  where user_id = %(user_id)s::uuid
                    and profile_id = %(profile_id)s::uuid
                  group by invoice_id
                ) pay on pay.invoice_id = i.id
            """
            paid_amount_expr = "coalesce(pay.paid_amount, 0)"

        if has_due_amount:
            due_amount_expr = (
                f"coalesce(i.due_amount, greatest(coalesce(i.total, 0) - ({paid_amount_expr}), 0))"
            )
        else:
            due_amount_expr = f"greatest(coalesce(i.total, 0) - ({paid_amount_expr}), 0)"

        with conn.cursor() as cur:
            cur.execute(
                f"""
                select
                  coalesce(count(*), 0) as outstanding_count,
                  coalesce(sum({due_amount_expr}), 0) as outstanding_due
                from {invoices_relation} i
                {join_payments_sql}
                where i.user_id = %(user_id)s::uuid
                  and i.profile_id = %(profile_id)s::uuid
                  and i.payment_status in ('partial', 'due')
                """,
                {"user_id": user_id, "profile_id": profile_id},
            )
            row = cur.fetchone() or {}
            snapshot["outstanding_invoice_count"] = int(row.get("outstanding_count") or 0)
            snapshot["outstanding_invoice_due_total"] = float(row.get("outstanding_due") or 0)

    if products_relation:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                select
                  coalesce(count(*), 0) as total_products,
                  coalesce(sum(coalesce(quantity, 0)), 0) as total_units
                from {products_relation}
                where user_id = %(user_id)s::uuid
                  and profile_id = %(profile_id)s::uuid
                  and is_active = true
                """,
                {"user_id": user_id, "profile_id": profile_id},
            )
            row = cur.fetchone() or {}
            snapshot["stock_product_count"] = int(row.get("total_products") or 0)
            snapshot["stock_units_total"] = float(row.get("total_units") or 0)

    if postings_relation and customers_relation:
        has_customer_active = _relation_has_column(conn, customers_relation, "is_active")
        customer_active_filter = "and c.is_active = true" if has_customer_active else ""
        customer_join_entries = ""
        customer_last_activity_expr = "null::text as last_activity_date"
        if entries_relation:
            customer_join_entries = f"left join {entries_relation} le on le.id = lp.entry_id"
            customer_last_activity_expr = "max(le.date)::text as last_activity_date"
        with conn.cursor() as cur:
            cur.execute(
                f"""
                select
                  c.id::text as customer_id,
                  coalesce(nullif(trim(c.name), ''), 'Customer') as customer_name,
                  coalesce(
                    sum(
                      case
                        when lp.direction='debit' then lp.amount
                        when lp.direction='credit' then -lp.amount
                        else 0
                      end
                    ),
                    0
                  ) as due_amount,
                  {customer_last_activity_expr}
                from {customers_relation} c
                left join {postings_relation} lp
                  on lp.user_id = c.user_id
                 and lp.profile_id = c.profile_id
                 and lp.leg_type = 'receivable'
                 and lp.ref_id = c.id
                {customer_join_entries}
                where c.user_id = %(user_id)s::uuid
                  and c.profile_id = %(profile_id)s::uuid
                  {customer_active_filter}
                group by c.id, c.name
                having coalesce(
                  sum(
                    case
                      when lp.direction='debit' then lp.amount
                      when lp.direction='credit' then -lp.amount
                      else 0
                    end
                  ),
                  0
                ) > 0
                order by due_amount desc, customer_name asc
                limit 100
                """,
                {"user_id": user_id, "profile_id": profile_id},
            )
            customer_rows = cur.fetchall() or []
        customer_breakdown: list[dict[str, object]] = []
        customer_due_by_name: dict[str, float] = {}
        for row in customer_rows:
            customer_name = _sanitize_text(row.get("customer_name"), "Customer")
            due_amount = float(row.get("due_amount") or 0)
            customer_breakdown.append(
                {
                    "customer_id": _sanitize_text(row.get("customer_id")) or None,
                    "customer_name": customer_name,
                    "due_amount": due_amount,
                    "last_activity_date": _sanitize_text(row.get("last_activity_date")) or None,
                }
            )
            normalized_name = customer_name.strip().lower()
            if normalized_name:
                customer_due_by_name[normalized_name] = round(
                    float(customer_due_by_name.get(normalized_name, 0.0)) + due_amount,
                    2,
                )
        snapshot["customer_due_breakdown"] = customer_breakdown
        snapshot["customer_due_count"] = len(customer_breakdown)
        snapshot["customer_due_by_name"] = customer_due_by_name

    if postings_relation and suppliers_relation:
        has_supplier_active = _relation_has_column(conn, suppliers_relation, "is_active")
        supplier_active_filter = "and s.is_active = true" if has_supplier_active else ""
        supplier_join_entries = ""
        supplier_last_activity_expr = "null::text as last_activity_date"
        if entries_relation:
            supplier_join_entries = f"left join {entries_relation} le on le.id = lp.entry_id"
            supplier_last_activity_expr = "max(le.date)::text as last_activity_date"
        with conn.cursor() as cur:
            cur.execute(
                f"""
                select
                  s.id::text as supplier_id,
                  coalesce(nullif(trim(s.name), ''), 'Supplier') as supplier_name,
                  coalesce(
                    sum(
                      case
                        when lp.direction='credit' then lp.amount
                        when lp.direction='debit' then -lp.amount
                        else 0
                      end
                    ),
                    0
                  ) as due_amount,
                  {supplier_last_activity_expr}
                from {suppliers_relation} s
                left join {postings_relation} lp
                  on lp.user_id = s.user_id
                 and lp.profile_id = s.profile_id
                 and lp.leg_type = 'payable'
                 and lp.ref_id = s.id
                {supplier_join_entries}
                where s.user_id = %(user_id)s::uuid
                  and s.profile_id = %(profile_id)s::uuid
                  {supplier_active_filter}
                group by s.id, s.name
                having coalesce(
                  sum(
                    case
                      when lp.direction='credit' then lp.amount
                      when lp.direction='debit' then -lp.amount
                      else 0
                    end
                  ),
                  0
                ) > 0
                order by due_amount desc, supplier_name asc
                limit 100
                """,
                {"user_id": user_id, "profile_id": profile_id},
            )
            supplier_rows = cur.fetchall() or []
        supplier_breakdown: list[dict[str, object]] = []
        supplier_due_by_name: dict[str, float] = {}
        for row in supplier_rows:
            supplier_name = _sanitize_text(row.get("supplier_name"), "Supplier")
            due_amount = float(row.get("due_amount") or 0)
            supplier_breakdown.append(
                {
                    "supplier_id": _sanitize_text(row.get("supplier_id")) or None,
                    "supplier_name": supplier_name,
                    "due_amount": due_amount,
                    "last_activity_date": _sanitize_text(row.get("last_activity_date")) or None,
                }
            )
            normalized_name = supplier_name.strip().lower()
            if normalized_name:
                supplier_due_by_name[normalized_name] = round(
                    float(supplier_due_by_name.get(normalized_name, 0.0)) + due_amount,
                    2,
                )
        snapshot["supplier_due_breakdown"] = supplier_breakdown
        snapshot["supplier_due_count"] = len(supplier_breakdown)
        snapshot["supplier_due_by_name"] = supplier_due_by_name

    return snapshot
