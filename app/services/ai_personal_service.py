import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from psycopg import Connection

from app.core.config import Settings
from app.services.ai_history_service import fetch_recent_messages
from app.services.ai_transport_service import request_chat_completion

FALLBACK_NO_DATA_MESSAGE = (
    "I do not have enough personal finance data yet. Add a few transactions, then ask again."
)

FINANCE_QUERY_PATTERN = re.compile(
    r"\b(spend|spending|expense|expenses|income|budget|transaction|transactions|category|categories|balance|cashflow|saving|savings|report|analy[sz]e|insight|money|finance|financial|monthly|weekly|week|yearly|year|salary|bill|bills|rent|grocery|food|transport|shopping|medical|health|education|entertainment|compare|total|how much|this month|last month|this year|last year)\b",
    re.IGNORECASE,
)

SIMPLE_GREETING_PATTERN = re.compile(
    r"^(hi|hello|hey|hola|yo|namaste|namaskar|good morning|good afternoon|good evening)\b[!. ]*$",
    re.IGNORECASE,
)


@dataclass
class DateScope:
    label: str
    start: date
    end: date



def _today() -> date:
    return datetime.utcnow().date()



def parse_date_scope(query: str) -> DateScope | None:
    normalized = query.lower().strip()
    now = _today()

    if "this month" in normalized:
        start = date(now.year, now.month, 1)
        if now.month == 12:
            end = date(now.year + 1, 1, 1) - timedelta(days=1)
        else:
            end = date(now.year, now.month + 1, 1) - timedelta(days=1)
        return DateScope(label="this month", start=start, end=end)

    if "last month" in normalized or "previous month" in normalized:
        if now.month == 1:
            start = date(now.year - 1, 12, 1)
            end = date(now.year, 1, 1) - timedelta(days=1)
        else:
            start = date(now.year, now.month - 1, 1)
            end = date(now.year, now.month, 1) - timedelta(days=1)
        return DateScope(label="last month", start=start, end=end)

    if "this year" in normalized:
        return DateScope(label="this year", start=date(now.year, 1, 1), end=date(now.year, 12, 31))

    if "last year" in normalized or "previous year" in normalized:
        return DateScope(label="last year", start=date(now.year - 1, 1, 1), end=date(now.year - 1, 12, 31))

    if "today" in normalized:
        return DateScope(label="today", start=now, end=now)

    if "yesterday" in normalized:
        y = now - timedelta(days=1)
        return DateScope(label="yesterday", start=y, end=y)

    match = re.search(r"last\s+(\d{1,3})\s+days", normalized)
    if match:
        days = max(1, min(365, int(match.group(1))))
        start = now - timedelta(days=days - 1)
        return DateScope(label=f"last {days} days", start=start, end=now)

    return None



def _get_personal_profile_id(conn: Connection, user_id: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            select id::text as id
            from public.profiles
            where user_id = %(user_id)s::uuid
              and profile_type = 'personal'
            order by created_at asc
            limit 1
            """,
            {"user_id": user_id},
        )
        row = cur.fetchone() or {}
    profile_id = str(row.get("id") or "").strip()
    return profile_id or None



def _fetch_transactions(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str | None,
    scope: DateScope | None,
    limit: int = 500,
) -> list[dict]:
    where_parts = ["tfv.user_id = %(user_id)s::uuid", "tfv.txn_type in ('income', 'expense')"]
    bind: dict[str, object] = {"user_id": user_id, "limit": max(30, min(limit, 800))}

    if profile_id:
        where_parts.append("tfv.profile_id = %(profile_id)s::uuid")
        bind["profile_id"] = profile_id

    if scope:
        where_parts.append("tfv.date between %(start_date)s and %(end_date)s")
        bind["start_date"] = scope.start.isoformat()
        bind["end_date"] = scope.end.isoformat()

    with conn.cursor() as cur:
        cur.execute(
            f"""
            select
              tfv.txn_type,
              tfv.amount,
              tfv.date,
              tfv.category_name,
              tfv.description,
              tfv.account_name,
              tfv.counterparty_name
            from public.transaction_feed_view tfv
            where {' and '.join(where_parts)}
            order by tfv.date desc, tfv.created_at desc
            limit %(limit)s
            """,
            bind,
        )
        return cur.fetchall() or []



def _detect_mode(query: str) -> str:
    return "finance" if FINANCE_QUERY_PATTERN.search(query or "") else "general"



def _build_recent_history_text(history_rows: list[dict]) -> str:
    if not history_rows:
        return ""
    lines: list[str] = []
    for row in history_rows[-12:]:
        role = str(row.get("role") or "assistant").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        text = str(row.get("content") or "").strip()
        if not text:
            continue
        if len(text) > 520:
            text = f"{text[:517]}..."
        lines.append(f"{role}: {text}")
    return "\n".join(lines)



def _build_top_categories(rows: list[dict], txn_type: str, top_n: int = 5) -> list[tuple[str, float]]:
    totals: dict[str, float] = {}
    for row in rows:
        if str(row.get("txn_type") or "").strip() != txn_type:
            continue
        name = str(row.get("category_name") or "Uncategorized").strip() or "Uncategorized"
        amount = float(row.get("amount") or 0)
        totals[name] = totals.get(name, 0.0) + amount
    ranked = sorted(totals.items(), key=lambda item: item[1], reverse=True)
    return ranked[:top_n]



def _build_retrieval_matches(rows: list[dict], query: str, limit: int = 8) -> list[dict]:
    normalized = (query or "").strip().lower()
    if not normalized:
        return []

    scored: list[dict] = []
    for row in rows:
        haystack = (
            f"{row.get('description') or ''} "
            f"{row.get('category_name') or ''} "
            f"{row.get('counterparty_name') or ''}"
        ).lower()
        if normalized in haystack:
            scored.append(
                {
                    "txn_type": row.get("txn_type"),
                    "amount": float(row.get("amount") or 0),
                    "date": row.get("date"),
                    "category": row.get("category_name"),
                    "description": row.get("description"),
                }
            )

    return scored[: max(1, min(limit, 20))]



def build_personal_prompt(
    conn: Connection,
    *,
    user_id: str,
    thread_id: str,
    user_query: str,
) -> tuple[str, list[str]]:
    warnings: list[str] = []
    query = user_query.strip()
    mode = _detect_mode(query)

    if SIMPLE_GREETING_PATTERN.match(query):
        return (
            "User says hello. Reply warmly in 1-2 lines and mention you can help with spending insights, budgets, and summaries.",
            warnings,
        )

    scope = parse_date_scope(query)
    profile_id = _get_personal_profile_id(conn, user_id)
    if not profile_id:
        warnings.append("No personal profile found.")

    rows = _fetch_transactions(conn, user_id=user_id, profile_id=profile_id, scope=scope, limit=500)
    if mode == "finance" and not rows:
        return (FALLBACK_NO_DATA_MESSAGE, warnings)

    total_income = sum(float(row.get("amount") or 0) for row in rows if row.get("txn_type") == "income")
    total_expense = sum(float(row.get("amount") or 0) for row in rows if row.get("txn_type") == "expense")
    balance = total_income - total_expense

    top_expense_categories = _build_top_categories(rows, "expense", top_n=5)
    top_income_categories = _build_top_categories(rows, "income", top_n=5)
    retrieved_matches = _build_retrieval_matches(rows, query, limit=8)

    history_rows = fetch_recent_messages(
        conn,
        user_id=user_id,
        scope="personal",
        thread_id=thread_id,
        limit=10,
    )
    recent_history = _build_recent_history_text(history_rows)

    evidence = {
        "mode": mode,
        "scope": {
            "label": scope.label,
            "start": scope.start.isoformat(),
            "end": scope.end.isoformat(),
        }
        if scope
        else None,
        "summary": {
            "transactions_count": len(rows),
            "total_income": round(total_income, 2),
            "total_expense": round(total_expense, 2),
            "net_balance": round(balance, 2),
        },
        "top_expense_categories": [
            {"category": name, "amount": round(amount, 2)} for name, amount in top_expense_categories
        ],
        "top_income_categories": [
            {"category": name, "amount": round(amount, 2)} for name, amount in top_income_categories
        ],
        "matched_transactions": retrieved_matches,
    }

    prompt = (
        "You are arthaX Personal Finance AI.\n"
        "Strict rules:\n"
        "1) Use only the PERSONAL evidence provided below; do not invent figures.\n"
        "2) If data is insufficient, clearly say what is missing.\n"
        "3) Keep output practical and concise with bullet points when useful.\n"
        "4) Mention currency as NPR unless user requests otherwise.\n\n"
        f"User query:\n{query}\n\n"
        f"Recent conversation:\n{recent_history or '[none]'}\n\n"
        "Personal evidence JSON:\n"
        f"{json.dumps(evidence, ensure_ascii=True)}\n\n"
        "Answer now."
    )

    return prompt, warnings



def generate_personal_chat_reply(
    conn: Connection,
    *,
    settings: Settings,
    user_id: str,
    thread_id: str,
    user_query: str,
) -> tuple[str, dict | None, list[str]]:
    prompt, warnings = build_personal_prompt(
        conn,
        user_id=user_id,
        thread_id=thread_id,
        user_query=user_query,
    )

    if prompt == FALLBACK_NO_DATA_MESSAGE:
        return prompt, None, warnings

    reply, usage, upstream_warnings = request_chat_completion(
        scope="personal",
        prompt=prompt,
        settings=settings,
    )
    return reply, usage, [*warnings, *upstream_warnings]
