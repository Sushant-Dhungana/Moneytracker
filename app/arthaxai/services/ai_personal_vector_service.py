from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timezone

from psycopg import Connection
from psycopg import Error as PsycopgError
from psycopg.pq import TransactionStatus

from app.arthaxai.services.ai_business_vector_service import (
    _compute_content_hash,
    _first_existing_relation,
    _parse_as_of,
    _relation_has_column,
    _sanitize_text,
    _to_vector_literal,
    create_embedding,
)
from app.arthaxai.services.personal_finance_context_service import build_personal_finance_context
from app.core.config import Settings
from app.core.db import apply_db_auth_context
from app.repositories.accounts_repository import get_account_balances
from app.repositories.counterparties_repository import list_counterparty_positions

_PERSONAL_VECTOR_DOCS_RELATIONS = ["personal.vector_documents"]
_PERSONAL_VECTOR_JOBS_RELATIONS = ["personal.vector_jobs"]
_PERSONAL_VECTOR_MATCH_FUNCTIONS = ["personal.match_vector_documents"]

_PERSONAL_SOURCE_KIND_ALIAS: dict[str, set[str]] = {
    "personal_summary": {"personal_summary", "summary", "overview"},
    "transaction_entry": {"transaction_entry", "transaction", "history", "entry"},
    "counterparty_position": {"counterparty_position", "counterparty", "lend", "borrow", "payable", "receivable"},
    "category_summary": {"category_summary", "category", "categories"},
    "account_balance_snapshot": {"account_balance_snapshot", "account", "cash", "bank", "balance"},
}


def _reset_failed_transaction(conn: Connection) -> None:
    try:
        if conn.info.transaction_status == TransactionStatus.INERROR:
            conn.rollback()
    except Exception:
        pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slugify(value: str) -> str:
    normalized = "".join(ch.lower() if ch.isalnum() else "-" for ch in str(value or "").strip())
    normalized = "-".join(part for part in normalized.split("-") if part)
    return normalized or "unknown"


def _doc_key(source_kind: str, source_id: str, chunk_index: int) -> tuple[str, str, int]:
    return (source_kind, source_id, chunk_index)


def _source_kind_family(source_id: str) -> str | None:
    normalized = _sanitize_text(source_id)
    if ":" not in normalized:
        return None
    return normalized.split(":", 1)[0] or None


def _expand_source_kind_aliases(source_kinds: set[str]) -> list[str]:
    if not source_kinds:
        return []
    expanded: set[str] = set()
    for source_kind in source_kinds:
        expanded.update(_PERSONAL_SOURCE_KIND_ALIAS.get(source_kind, {source_kind}))
    return sorted(item for item in expanded if item)


def _infer_source_kinds_for_query(query: str) -> set[str]:
    normalized = _sanitize_text(query).lower()
    if not normalized:
        return set(_PERSONAL_SOURCE_KIND_ALIAS.keys())

    source_kinds: set[str] = set()
    if any(token in normalized for token in ["lend", "lent", "borrow", "borrowed", "loan", "payable", "receivable", "owe", "owed", "tirnu", "baki"]):
        source_kinds.add("counterparty_position")
        source_kinds.add("transaction_entry")
    if any(token in normalized for token in ["account", "cash", "bank", "balance", "how much do i have", "paisa", "money"]):
        source_kinds.add("account_balance_snapshot")
        source_kinds.add("personal_summary")
    if any(token in normalized for token in ["category", "categories", "food", "transport", "shopping", "expense category", "income category"]):
        source_kinds.add("category_summary")
    if any(token in normalized for token in ["transaction", "transactions", "history", "recent", "entry", "daraz", "esewa", "khalti", "nabil"]):
        source_kinds.add("transaction_entry")
    if any(token in normalized for token in ["summary", "overview", "income", "expense", "saving", "savings", "net"]):
        source_kinds.add("personal_summary")

    return source_kinds or set(_PERSONAL_SOURCE_KIND_ALIAS.keys())


def _fetch_personal_transaction_rows(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    entry_id: str | None = None,
) -> list[dict]:
    bind: dict[str, object] = {
        "user_id": user_id,
        "profile_id": profile_id,
    }
    entry_filter = ""
    normalized_entry_id = _sanitize_text(entry_id)
    if normalized_entry_id:
        entry_filter = "and tfv.ledger_entry_id = %(entry_id)s::uuid"
        bind["entry_id"] = normalized_entry_id

    with conn.cursor() as cur:
        cur.execute(
            f"""
            select
              tfv.ledger_entry_id::text as entry_id,
              tfv.txn_type,
              tfv.amount,
              tfv.date::text as entry_date,
              tfv.category_id::text as category_id,
              tfv.category_name,
              tfv.account_id::text as account_id,
              tfv.account_name,
              tfv.account_type,
              tfv.counterparty_id::text as counterparty_id,
              tfv.counterparty_name,
              tfv.description,
              tfv.metadata,
              tfv.created_at::text as created_at
            from public.transaction_feed_view tfv
            where tfv.user_id = %(user_id)s::uuid
              and tfv.profile_id = %(profile_id)s::uuid
              and tfv.txn_type in (
                'income',
                'expense',
                'transfer',
                'loan_out',
                'loan_in',
                'repayment_in',
                'repayment_out'
              )
              {entry_filter}
            order by tfv.date desc, tfv.created_at desc
            """,
            bind,
        )
        return cur.fetchall() or []


def _build_personal_summary_docs(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    as_of: str,
) -> list[dict]:
    finance_context = build_personal_finance_context(
        conn,
        user_id=user_id,
        profile_id=profile_id,
        start_date=None,
        end_date=None,
        query="personal summary",
    )
    income_statement = finance_context.get("income_statement") if isinstance(finance_context, dict) else {}
    cash_position = finance_context.get("cash_position") if isinstance(finance_context, dict) else {}

    total_income = float((income_statement or {}).get("total_income") or 0)
    total_expense = float((income_statement or {}).get("total_expense") or 0)
    net_savings = float((income_statement or {}).get("net_savings") or 0)
    cash_total = float((cash_position or {}).get("cash_total") or 0)
    bank_total = float((cash_position or {}).get("bank_total") or 0)
    savings_total = float((cash_position or {}).get("savings_total") or 0)
    other_total = float((cash_position or {}).get("other_total") or 0)

    return [
        {
            "source_kind": "personal_summary",
            "source_id": "summary:global",
            "chunk_index": 0,
            "schema_version": "v1",
            "as_of": as_of,
            "content": (
                "Personal finance summary. "
                f"Total income NPR {total_income:.2f}. "
                f"Total expense NPR {total_expense:.2f}. "
                f"Net savings NPR {net_savings:.2f}. "
                f"Cash balance NPR {cash_total:.2f}. "
                f"Bank balance NPR {bank_total:.2f}. "
                f"Savings balance NPR {savings_total:.2f}. "
                f"Other balance NPR {other_total:.2f}."
            ),
            "metadata": {
                "as_of": as_of,
                "total_income": total_income,
                "total_expense": total_expense,
                "net_savings": net_savings,
                "cash_total": cash_total,
                "bank_total": bank_total,
                "savings_total": savings_total,
                "other_total": other_total,
                "authoritative_numeric": False,
            },
        }
    ]


def _build_personal_category_summary_docs(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    as_of: str,
) -> list[dict]:
    finance_context = build_personal_finance_context(
        conn,
        user_id=user_id,
        profile_id=profile_id,
        start_date=None,
        end_date=None,
        query="category summary",
    )
    category_breakdown = (
        finance_context.get("category_breakdown")
        if isinstance(finance_context, dict) and isinstance(finance_context.get("category_breakdown"), dict)
        else {}
    )

    docs: list[dict] = []
    for txn_type, rows in (
        ("income", list(category_breakdown.get("income_by_category") or [])),
        ("expense", list(category_breakdown.get("expense_by_category") or [])),
    ):
        for row in rows:
            category_name = _sanitize_text(row.get("category"), "Uncategorized")
            amount = float(row.get("amount") or 0)
            pct_of_total = row.get("pct_of_total")
            txn_count = int(row.get("txn_count") or 0)
            docs.append(
                {
                    "source_kind": "category_summary",
                    "source_id": f"{txn_type}:{_slugify(category_name)}",
                    "chunk_index": 0,
                    "schema_version": "v1",
                    "as_of": as_of,
                    "content": (
                        f"Personal {txn_type} category summary. "
                        f"Category {category_name}. "
                        f"Total amount NPR {amount:.2f}. "
                        f"Transaction count {txn_count}. "
                        f"Share of total {round(float(pct_of_total or 0) * 100, 1):.1f} percent."
                    ),
                    "metadata": {
                        "as_of": as_of,
                        "txn_type": txn_type,
                        "category_name": category_name,
                        "amount": amount,
                        "txn_count": txn_count,
                        "pct_of_total": pct_of_total,
                        "authoritative_numeric": False,
                    },
                }
            )
    return docs


def _build_personal_account_balance_docs(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    as_of: str,
) -> list[dict]:
    rows = get_account_balances(conn, user_id, profile_id)
    docs: list[dict] = []
    for row in rows:
        account_id = _sanitize_text(row.get("account_id"))
        account_name = _sanitize_text(row.get("account_name"), "Account")
        account_type = _sanitize_text(row.get("account_type"), "other")
        current_balance = float(row.get("current_balance") or 0)
        opening_balance = float(row.get("opening_balance") or 0)
        docs.append(
            {
                "source_kind": "account_balance_snapshot",
                "source_id": f"account:{account_id or _slugify(account_name)}",
                "chunk_index": 0,
                "schema_version": "v1",
                "as_of": as_of,
                "content": (
                    "Personal account balance snapshot. "
                    f"Account {account_name}. "
                    f"Account type {account_type}. "
                    f"Current balance NPR {current_balance:.2f}. "
                    f"Opening balance NPR {opening_balance:.2f}."
                ),
                "metadata": {
                    "as_of": as_of,
                    "account_id": account_id or None,
                    "account_name": account_name,
                    "account_type": account_type,
                    "current_balance": current_balance,
                    "opening_balance": opening_balance,
                    "authoritative_numeric": False,
                },
            }
        )
    return docs


def _build_personal_counterparty_position_docs(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    as_of: str,
) -> list[dict]:
    rows = list_counterparty_positions(conn, user_id=user_id, profile_id=profile_id)
    docs: list[dict] = []
    for row in rows:
        counterparty_id = _sanitize_text(row.get("counterparty_id"))
        name = _sanitize_text(row.get("name"), "Counterparty")
        relation_type = _sanitize_text(row.get("relation_type"), "person")
        opening_balance = float(row.get("opening_balance") or 0)
        receivable = float(row.get("receivable") or 0)
        payable = float(row.get("payable") or 0)
        net_position = float(row.get("net_position") or 0)

        if receivable > 0 and payable <= 0:
            balance_text = f"This person owes you NPR {receivable:.2f}."
        elif payable > 0 and receivable <= 0:
            balance_text = f"You owe this person NPR {payable:.2f}."
        elif receivable <= 0 and payable <= 0:
            balance_text = "There is no receivable balance and no payable balance right now."
        else:
            balance_text = (
                f"This balance includes receivable NPR {receivable:.2f} "
                f"and payable NPR {payable:.2f}."
            )

        docs.append(
            {
                "source_kind": "counterparty_position",
                "source_id": f"counterparty:{counterparty_id or _slugify(name)}",
                "chunk_index": 0,
                "schema_version": "v1",
                "as_of": as_of,
                "content": (
                    "Personal counterparty position. "
                    f"Counterparty {name}. "
                    f"Relation type {relation_type}. "
                    f"Opening balance NPR {opening_balance:.2f}. "
                    f"Receivable NPR {receivable:.2f}. "
                    f"Payable NPR {payable:.2f}. "
                    f"Net position NPR {net_position:.2f}. "
                    f"{balance_text}"
                ),
                "metadata": {
                    "as_of": as_of,
                    "counterparty_id": counterparty_id or None,
                    "counterparty_name": name,
                    "relation_type": relation_type,
                    "opening_balance": opening_balance,
                    "receivable": receivable,
                    "payable": payable,
                    "net_position": net_position,
                    "authoritative_numeric": False,
                },
            }
        )
    return docs


def _build_personal_transaction_docs(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    as_of: str,
    entry_id: str | None = None,
) -> list[dict]:
    rows = _fetch_personal_transaction_rows(
        conn,
        user_id=user_id,
        profile_id=profile_id,
        entry_id=entry_id,
    )

    docs: list[dict] = []
    for row in rows:
        normalized_entry_id = _sanitize_text(row.get("entry_id"), "entry")
        txn_type = _sanitize_text(row.get("txn_type"), "transaction")
        amount = float(row.get("amount") or 0)
        entry_date = _sanitize_text(row.get("entry_date"))
        category_name = _sanitize_text(row.get("category_name"))
        account_name = _sanitize_text(row.get("account_name"))
        account_type = _sanitize_text(row.get("account_type"))
        counterparty_name = _sanitize_text(row.get("counterparty_name"))
        description = _sanitize_text(row.get("description"))
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}

        direction_sentence = ""
        if txn_type == "loan_out":
            direction_sentence = "You lent money to this counterparty."
        elif txn_type == "loan_in":
            direction_sentence = "You borrowed money from this counterparty."
        elif txn_type == "repayment_in":
            direction_sentence = "This counterparty paid you back."
        elif txn_type == "repayment_out":
            direction_sentence = "You paid this counterparty back."
        elif txn_type == "income":
            direction_sentence = "This was money received."
        elif txn_type == "expense":
            direction_sentence = "This was money paid."
        elif txn_type == "transfer":
            direction_sentence = "This was a transfer between your accounts."

        content_parts = [
            "Personal transaction entry.",
            f"Transaction type {txn_type.replace('_', ' ')}.",
            f"Amount NPR {amount:.2f}.",
            f"Date {entry_date}.",
        ]
        if category_name:
            content_parts.append(f"Category {category_name}.")
        if account_name:
            content_parts.append(f"Account {account_name}.")
        if account_type:
            content_parts.append(f"Account type {account_type}.")
        if counterparty_name:
            content_parts.append(f"Counterparty {counterparty_name}.")
        if description:
            content_parts.append(f"Notes {description}.")
        if direction_sentence:
            content_parts.append(direction_sentence)

        docs.append(
            {
                "source_kind": "transaction_entry",
                "source_id": f"entry:{normalized_entry_id}",
                "chunk_index": 0,
                "schema_version": "v1",
                "as_of": as_of,
                "content": " ".join(content_parts),
                "metadata": {
                    "as_of": as_of,
                    "entry_id": normalized_entry_id,
                    "txn_type": txn_type,
                    "amount": amount,
                    "entry_date": entry_date or None,
                    "category_id": _sanitize_text(row.get("category_id")) or None,
                    "category_name": category_name or None,
                    "account_id": _sanitize_text(row.get("account_id")) or None,
                    "account_name": account_name or None,
                    "counterparty_id": _sanitize_text(row.get("counterparty_id")) or None,
                    "counterparty_name": counterparty_name or None,
                    "metadata": metadata or None,
                    "authoritative_numeric": False,
                },
            }
        )
    return docs


def _collect_personal_documents(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
) -> list[dict]:
    as_of = _now_iso()
    docs: list[dict] = []
    docs.extend(_build_personal_summary_docs(conn, user_id=user_id, profile_id=profile_id, as_of=as_of))
    docs.extend(_build_personal_category_summary_docs(conn, user_id=user_id, profile_id=profile_id, as_of=as_of))
    docs.extend(_build_personal_account_balance_docs(conn, user_id=user_id, profile_id=profile_id, as_of=as_of))
    docs.extend(_build_personal_counterparty_position_docs(conn, user_id=user_id, profile_id=profile_id, as_of=as_of))
    docs.extend(_build_personal_transaction_docs(conn, user_id=user_id, profile_id=profile_id, as_of=as_of))
    return docs


def _upsert_personal_documents_incremental(
    conn: Connection,
    *,
    settings: Settings,
    user_id: str,
    profile_id: str,
    docs: list[dict],
) -> dict[str, int]:
    docs_relation = _first_existing_relation(conn, _PERSONAL_VECTOR_DOCS_RELATIONS)
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
        source_kind = _sanitize_text(raw.get("source_kind"))
        source_id = _sanitize_text(raw.get("source_id"))
        content = _sanitize_text(raw.get("content"))
        chunk_index = int(raw.get("chunk_index") or 0)
        if not source_kind or not source_id or not content:
            continue
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        schema_version = _sanitize_text(raw.get("schema_version"), "v1")
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
              {"schema_version" if has_schema_version else "'v1'::text"} as schema_version,
              {"as_of::text" if has_as_of else "null::text"} as as_of
            from {docs_relation}
            where user_id = %(user_id)s::uuid
              and profile_id = %(profile_id)s::uuid
            """,
            {"user_id": user_id, "profile_id": profile_id},
        )
        for row in cur.fetchall() or []:
            existing_by_key[_doc_key(
                _sanitize_text(row.get("source_kind")),
                _sanitize_text(row.get("source_id")),
                int(row.get("chunk_index") or 0),
            )] = row

    changed_docs: list[dict] = []
    desired_keys: set[tuple[str, str, int]] = set()
    for doc in normalized_docs:
        key = _doc_key(doc["source_kind"], doc["source_id"], doc["chunk_index"])
        desired_keys.add(key)
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
        existing_as_of = _parse_as_of(_sanitize_text(existing.get("as_of")))
        doc_as_of = _parse_as_of(doc.get("as_of"))
        if existing_as_of and doc_as_of and existing_as_of > doc_as_of:
            continue
        if existing_hash != doc["content_hash"] or bool(existing.get("is_tombstone")):
            changed_docs.append(doc)

    tombstoned_docs = 0
    if has_is_tombstone:
        for key, row in existing_by_key.items():
            if key in desired_keys:
                continue
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    update {docs_relation}
                    set
                      is_tombstone = true,
                      embedding = null,
                      updated_at = now(),
                      {"deleted_at = now()," if has_deleted_at else ""}
                      content = content
                    where user_id = %(user_id)s::uuid
                      and profile_id = %(profile_id)s::uuid
                      and source_kind = %(source_kind)s::text
                      and source_id = %(source_id)s::text
                      and chunk_index = %(chunk_index)s::int
                    """,
                    {
                        "user_id": user_id,
                        "profile_id": profile_id,
                        "source_kind": key[0],
                        "source_id": key[1],
                        "chunk_index": key[2],
                    },
                )
            tombstoned_docs += 1

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
        update_set.append("deleted_at = null")

    for doc in changed_docs:
        embedding_value = doc.get("embedding")
        with conn.cursor() as cur:
            cur.execute(
                f"""
                insert into {docs_relation} (
                  {", ".join(insert_columns)}
                ) values (
                  {", ".join(insert_values)}
                )
                on conflict (user_id, profile_id, source_kind, source_id, chunk_index)
                do update set
                  {", ".join(update_set)}
                """,
                {
                    "user_id": user_id,
                    "profile_id": profile_id,
                    "source_kind": doc["source_kind"],
                    "source_id": doc["source_id"],
                    "chunk_index": doc["chunk_index"],
                    "content": doc["content"],
                    "metadata": json.dumps(doc["metadata"]),
                    "embedding": _to_vector_literal(embedding_value) if isinstance(embedding_value, list) else None,
                    "content_hash": doc["content_hash"],
                    "schema_version": doc["schema_version"],
                    "embed_model": settings.embedding_model_id if embedding_value else None,
                    "embed_version": "v1" if embedding_value else None,
                    "as_of": doc["as_of"],
                },
            )

    return {
        "total_docs": len(normalized_docs),
        "changed_docs": len(changed_docs),
        "embedded_docs": embedded_docs,
        "tombstoned_docs": tombstoned_docs,
    }


def enqueue_personal_ai_refresh_job(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    source_kind: str = "full_refresh",
    source_id: str = "*",
) -> None:
    jobs_relation = _first_existing_relation(conn, _PERSONAL_VECTOR_JOBS_RELATIONS)
    if not jobs_relation:
        return

    has_next_attempt_at = _relation_has_column(conn, jobs_relation, "next_attempt_at")
    with conn.cursor() as cur:
        if has_next_attempt_at:
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
            on conflict (user_id, profile_id, source_kind, source_id)
            do update set
              status = 'pending',
              last_error = null,
              updated_at = now()
            """,
            {
                "user_id": user_id,
                "profile_id": profile_id,
                "source_kind": source_kind,
                "source_id": source_id,
            },
        )


def upsert_personal_summary_doc(
    conn: Connection,
    *,
    settings: Settings,
    user_id: str,
    profile_id: str,
) -> dict[str, int]:
    docs = _build_personal_summary_docs(conn, user_id=user_id, profile_id=profile_id, as_of=_now_iso())
    return _upsert_personal_documents_incremental(
        conn,
        settings=settings,
        user_id=user_id,
        profile_id=profile_id,
        docs=docs,
    )


def upsert_personal_transaction_entry_doc(
    conn: Connection,
    *,
    settings: Settings,
    user_id: str,
    profile_id: str,
    entry_id: str,
) -> dict[str, int]:
    docs = _build_personal_transaction_docs(
        conn,
        user_id=user_id,
        profile_id=profile_id,
        as_of=_now_iso(),
        entry_id=entry_id,
    )
    if not docs:
        tombstone_personal_transaction_entry_doc(
            conn,
            user_id=user_id,
            profile_id=profile_id,
            entry_id=entry_id,
        )
        return {"total_docs": 0, "changed_docs": 0, "embedded_docs": 0, "tombstoned_docs": 1}
    return _upsert_personal_documents_incremental(
        conn,
        settings=settings,
        user_id=user_id,
        profile_id=profile_id,
        docs=docs,
    )


def upsert_personal_counterparty_position_docs(
    conn: Connection,
    *,
    settings: Settings,
    user_id: str,
    profile_id: str,
) -> dict[str, int]:
    docs = _build_personal_counterparty_position_docs(
        conn,
        user_id=user_id,
        profile_id=profile_id,
        as_of=_now_iso(),
    )
    return _upsert_personal_documents_incremental(
        conn,
        settings=settings,
        user_id=user_id,
        profile_id=profile_id,
        docs=docs,
    )


def upsert_personal_category_summary_docs(
    conn: Connection,
    *,
    settings: Settings,
    user_id: str,
    profile_id: str,
) -> dict[str, int]:
    docs = _build_personal_category_summary_docs(
        conn,
        user_id=user_id,
        profile_id=profile_id,
        as_of=_now_iso(),
    )
    return _upsert_personal_documents_incremental(
        conn,
        settings=settings,
        user_id=user_id,
        profile_id=profile_id,
        docs=docs,
    )


def upsert_personal_account_balance_docs(
    conn: Connection,
    *,
    settings: Settings,
    user_id: str,
    profile_id: str,
) -> dict[str, int]:
    docs = _build_personal_account_balance_docs(
        conn,
        user_id=user_id,
        profile_id=profile_id,
        as_of=_now_iso(),
    )
    return _upsert_personal_documents_incremental(
        conn,
        settings=settings,
        user_id=user_id,
        profile_id=profile_id,
        docs=docs,
    )


def tombstone_personal_transaction_entry_doc(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    entry_id: str,
) -> None:
    docs_relation = _first_existing_relation(conn, _PERSONAL_VECTOR_DOCS_RELATIONS)
    if not docs_relation:
        return
    has_deleted_at = _relation_has_column(conn, docs_relation, "deleted_at")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            update {docs_relation}
            set
              is_tombstone = true,
              embedding = null,
              updated_at = now()
              {", deleted_at = now()" if has_deleted_at else ""}
            where user_id = %(user_id)s::uuid
              and profile_id = %(profile_id)s::uuid
              and source_kind = 'transaction_entry'
              and source_id = %(source_id)s::text
              and chunk_index = 0
            """,
            {
                "user_id": user_id,
                "profile_id": profile_id,
                "source_id": f"entry:{_sanitize_text(entry_id)}",
            },
        )


def _refresh_all_personal_documents(
    conn: Connection,
    *,
    settings: Settings,
    user_id: str,
    profile_id: str,
) -> dict[str, int]:
    docs = _collect_personal_documents(conn, user_id=user_id, profile_id=profile_id)
    return _upsert_personal_documents_incremental(
        conn,
        settings=settings,
        user_id=user_id,
        profile_id=profile_id,
        docs=docs,
    )


def process_due_personal_vector_jobs(
    conn: Connection,
    *,
    settings: Settings,
    max_profiles: int = 5,
    max_jobs_per_profile: int = 2,
) -> list[str]:
    warnings: list[str] = []
    jobs_relation = _first_existing_relation(conn, _PERSONAL_VECTOR_JOBS_RELATIONS)
    if not jobs_relation:
        return warnings

    with conn.cursor() as cur:
        cur.execute(
            f"""
            select user_id::text as user_id, profile_id::text as profile_id
            from {jobs_relation}
            where status = 'pending'
              and next_attempt_at <= now()
            group by user_id, profile_id
            order by min(created_at) asc
            limit %(limit)s::int
            """,
            {"limit": max_profiles},
        )
        profile_rows = cur.fetchall() or []

    for profile_row in profile_rows:
        user_id = _sanitize_text(profile_row.get("user_id"))
        profile_id = _sanitize_text(profile_row.get("profile_id"))
        if not user_id or not profile_id:
            continue

        apply_db_auth_context(conn, user_id)
        with conn.cursor() as cur:
            cur.execute(
                f"""
                update {jobs_relation}
                set status = 'running', attempts = attempts + 1, updated_at = now()
                where id in (
                  select id
                  from {jobs_relation}
                  where user_id = %(user_id)s::uuid
                    and profile_id = %(profile_id)s::uuid
                    and status = 'pending'
                    and next_attempt_at <= now()
                  order by created_at asc
                  limit %(limit)s::int
                )
                returning id
                """,
                {"user_id": user_id, "profile_id": profile_id, "limit": max_jobs_per_profile},
            )
            running_jobs = cur.fetchall() or []

        if not running_jobs:
            continue

        try:
            stats = _refresh_all_personal_documents(
                conn,
                settings=settings,
                user_id=user_id,
                profile_id=profile_id,
            )
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    update {jobs_relation}
                    set status = 'done', last_error = null, updated_at = now()
                    where user_id = %(user_id)s::uuid
                      and profile_id = %(profile_id)s::uuid
                      and status = 'running'
                    """,
                    {"user_id": user_id, "profile_id": profile_id},
                )
            conn.commit()
            print(f"[PersonalVectorWorker] refreshed docs for {profile_id} (changed={stats.get('changed_docs', 0)})")
        except Exception as exc:
            _reset_failed_transaction(conn)
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    update {jobs_relation}
                    set
                      status = 'pending',
                      last_error = %(last_error)s::text,
                      next_attempt_at = now() + interval '1 minute',
                      updated_at = now()
                    where user_id = %(user_id)s::uuid
                      and profile_id = %(profile_id)s::uuid
                      and status = 'running'
                    """,
                    {
                        "user_id": user_id,
                        "profile_id": profile_id,
                        "last_error": str(exc),
                    },
                )
            conn.commit()
            warnings.append("Personal vector indexing job failed; retry scheduled.")
    return warnings


def _count_personal_docs(conn: Connection, *, user_id: str, profile_id: str) -> int:
    docs_relation = _first_existing_relation(conn, _PERSONAL_VECTOR_DOCS_RELATIONS)
    if not docs_relation:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            f"""
            select count(*) as total
            from {docs_relation}
            where user_id = %(user_id)s::uuid
              and profile_id = %(profile_id)s::uuid
              and coalesce(is_tombstone, false) = false
            """,
            {"user_id": user_id, "profile_id": profile_id},
        )
        row = cur.fetchone() or {}
    return int(row.get("total") or 0)


def _count_pending_personal_jobs(conn: Connection, *, user_id: str, profile_id: str) -> int:
    jobs_relation = _first_existing_relation(conn, _PERSONAL_VECTOR_JOBS_RELATIONS)
    if not jobs_relation:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            f"""
            select count(*) as total
            from {jobs_relation}
            where user_id = %(user_id)s::uuid
              and profile_id = %(profile_id)s::uuid
              and status = 'pending'
              and next_attempt_at <= now()
            """,
            {"user_id": user_id, "profile_id": profile_id},
        )
        row = cur.fetchone() or {}
    return int(row.get("total") or 0)


def get_personal_vector_status_warnings(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
) -> list[str]:
    warnings: list[str] = []
    docs_relation = _first_existing_relation(conn, _PERSONAL_VECTOR_DOCS_RELATIONS)
    if not docs_relation:
        return ["Personal vector store is unavailable. Apply the latest Supabase migrations."]

    if _count_personal_docs(conn, user_id=user_id, profile_id=profile_id) == 0:
        warnings.append("Personal semantic index has no documents yet. Answers will use SQL evidence first.")
    if _count_pending_personal_jobs(conn, user_id=user_id, profile_id=profile_id) > 0:
        warnings.append("Personal semantic index update is pending. Context may lag behind recent writes.")
    return warnings


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
    docs_relation = _first_existing_relation(conn, _PERSONAL_VECTOR_DOCS_RELATIONS)
    if not docs_relation:
        return []

    query_embedding = create_embedding(text=query, settings=settings)
    if not query_embedding:
        return []

    source_filter = ""
    bind: dict[str, object] = {
        "user_id": user_id,
        "profile_id": profile_id,
        "query_embedding": _to_vector_literal(query_embedding),
        "threshold": threshold,
        "limit": limit,
    }
    if source_kinds:
        source_filter = "and d.source_kind = any(%(source_kinds)s::text[])"
        bind["source_kinds"] = source_kinds

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
              extract(epoch from coalesce(d.indexed_at, d.updated_at, d.created_at)) as recency_epoch
            from {docs_relation} d
            where d.user_id = %(user_id)s::uuid
              and d.profile_id = %(profile_id)s::uuid
              and coalesce(d.is_tombstone, false) = false
              and d.embedding is not null
              {source_filter}
              and 1 - (d.embedding OPERATOR(extensions.<=>) %(query_embedding)s::extensions.vector) >= %(threshold)s::float
            order by
              d.embedding OPERATOR(extensions.<=>) %(query_embedding)s::extensions.vector,
              coalesce(d.indexed_at, d.updated_at, d.created_at) desc
            limit %(limit)s::int
            """,
            bind,
        )
        return cur.fetchall() or []


def _fetch_lexical_candidates(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    query: str,
    source_kinds: list[str],
    limit: int,
) -> list[dict]:
    docs_relation = _first_existing_relation(conn, _PERSONAL_VECTOR_DOCS_RELATIONS)
    if not docs_relation:
        return []

    query_like = f"%{_sanitize_text(query)}%"
    source_filter = ""
    bind: dict[str, object] = {
        "user_id": user_id,
        "profile_id": profile_id,
        "query": _sanitize_text(query),
        "query_like": query_like,
        "limit": limit,
    }
    if source_kinds:
        source_filter = "and d.source_kind = any(%(source_kinds)s::text[])"
        bind["source_kinds"] = source_kinds

    with conn.cursor() as cur:
        cur.execute(
            f"""
            select
              d.source_kind,
              d.source_id,
              d.chunk_index,
              d.content,
              d.metadata,
              ts_rank_cd(
                to_tsvector('simple', coalesce(d.content, '')),
                websearch_to_tsquery('simple', %(query)s)
              ) as similarity,
              extract(epoch from coalesce(d.indexed_at, d.updated_at, d.created_at)) as recency_epoch
            from {docs_relation} d
            where d.user_id = %(user_id)s::uuid
              and d.profile_id = %(profile_id)s::uuid
              and coalesce(d.is_tombstone, false) = false
              {source_filter}
              and (
                to_tsvector('simple', coalesce(d.content, '')) @@ websearch_to_tsquery('simple', %(query)s)
                or d.content ilike %(query_like)s
              )
            order by similarity desc, coalesce(d.indexed_at, d.updated_at, d.created_at) desc
            limit %(limit)s::int
            """,
            bind,
        )
        return cur.fetchall() or []


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

    def _merge_row(row: dict, rank: int, channel: str) -> None:
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

        score = 1.0 / (60.0 + float(rank))
        similarity_bonus = max(0.0, min(float(row.get("similarity") or 0), 1.0))
        score += 0.08 * similarity_bonus if channel == "vector" else 0.05 * similarity_bonus
        recency_epoch = float(row.get("recency_epoch") or 0)
        if recency_epoch > 0:
            age_days = max(0.0, (now_epoch - recency_epoch) / 86400.0)
            score += 0.02 * (1.0 / (1.0 + age_days))
        scores[key] += score

    for index, row in enumerate(vector_rows, start=1):
        _merge_row(row, index, "vector")
    for index, row in enumerate(lexical_rows, start=1):
        _merge_row(row, index, "lexical")

    ranked_keys = sorted(scores.keys(), key=lambda item: scores[item], reverse=True)
    selected: list[dict] = []
    deferred: list[dict] = []
    for key in ranked_keys:
        row = by_key[key]
        source_kind = _sanitize_text(row.get("source_kind"))
        source_family = _source_kind_family(_sanitize_text(row.get("source_id")))
        if kind_counts[source_kind] >= 3:
            continue
        if source_family and family_counts[source_family] >= 2:
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
        if kind_counts[source_kind] >= 3:
            continue
        selected.append(row)
        kind_counts[source_kind] += 1
        if len(selected) >= final_limit:
            return selected
    return selected


def retrieve_personal_vector_matches(
    conn: Connection,
    *,
    settings: Settings,
    user_id: str,
    profile_id: str,
    query: str,
    match_threshold: float = 0.55,
    match_count: int = 8,
) -> list[dict]:
    docs_relation = _first_existing_relation(conn, _PERSONAL_VECTOR_DOCS_RELATIONS)
    if not docs_relation:
        return []

    normalized_query = _sanitize_text(query)
    if not normalized_query:
        return []

    allowed_source_kinds = _expand_source_kind_aliases(_infer_source_kinds_for_query(normalized_query))
    safe_threshold = max(0.0, min(float(match_threshold), 1.0))
    safe_final_count = max(1, min(int(match_count), 20))
    candidate_limit = max(24, safe_final_count * 3)

    vector_rows = _fetch_vector_candidates(
        conn,
        settings=settings,
        user_id=user_id,
        profile_id=profile_id,
        query=normalized_query,
        source_kinds=allowed_source_kinds,
        threshold=safe_threshold,
        limit=candidate_limit,
    )
    lexical_rows = _fetch_lexical_candidates(
        conn,
        user_id=user_id,
        profile_id=profile_id,
        query=normalized_query,
        source_kinds=allowed_source_kinds,
        limit=candidate_limit,
    )
    return _fuse_candidates(
        vector_rows=vector_rows,
        lexical_rows=lexical_rows,
        final_limit=safe_final_count,
    )
