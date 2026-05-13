from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import re
from difflib import SequenceMatcher

from psycopg import Connection

from app.arthaxai.chat.intent_router import route_personal_chat_intent
from app.arthaxai.chat.neplish import detect_response_language_mode, normalize_neplish_text
from app.arthaxai.chat.response_formatter import format_personal_reply_like_business_structure
from app.arthaxai.tools.personal_vector_tools import get_personal_vector_context
from app.arthaxai.services.accounting_statement_service import (
    get_personal_to_business_handoff_message,
    is_business_question_for_personal_chat,
)
from app.arthaxai.services.ai_personal_query_service import (
    parse_personal_query_understanding,
    try_generate_deterministic_personal_response,
)
# personal finance context service — mirrors accounting_statement_service for personal data
from app.arthaxai.services.personal_finance_context_service import build_personal_finance_context
from app.core.config import Settings
from app.repositories.counterparties_repository import list_counterparty_positions

FALLBACK_NO_DATA_MESSAGE = (
    "I do not have enough personal finance data yet. Add a few transactions, then ask again."
)

SIMPLE_GREETING_PATTERN = re.compile(
    r"^(hi|hello|hey|hola|yo|namaste|namaskar|good morning|good afternoon|good evening)\b[!. ]*$",
    re.IGNORECASE,
)
PERSONAL_TRANSACTION_HISTORY_PATTERN = re.compile(
    r"\b(transaction|transactions|transaction history|history|ledger|recent transactions)\b",
    re.IGNORECASE,
)
PERSONAL_COUNTERPARTY_QUERY_PATTERN = re.compile(
    r"\b(paid|pay|spend|spent|give|gave|owe|owed|should i pay|received|receive|got|from|to|with|"
    r"tirnu|tirna|tiryo|tiris|tirne|dinu|linu|baki|lend|lent|borrow|borrowed|loan|udhar|rin|sapati|"
    r"receivable|payable)\b",
    re.IGNORECASE,
)

PERSONAL_QUERY_STOPWORDS = {
    "show",
    "give",
    "me",
    "my",
    "mine",
    "please",
    "transaction",
    "transactions",
    "history",
    "total",
    "all",
    "full",
    "recent",
    "latest",
    "personal",
    "expense",
    "expenses",
    "income",
    "summary",
    "report",
    "for",
    "of",
    "with",
    "ko",
    "sanga",
    "this",
    "last",
    "month",
    "week",
    "year",
    "today",
    "yesterday",
    "how",
    "much",
    "who",
    "which",
    "person",
    "persons",
    "people",
    "name",
    "names",
    "pay",
    "paid",
    "owe",
    "owed",
    "tirnu",
    "tirna",
    "tiryo",
    "tiris",
    "tirne",
    "dinu",
    "linu",
    "lend",
    "lent",
    "borrow",
    "borrowed",
    "loan",
    "udhar",
    "rin",
    "sapati",
    "receivable",
    "payable",
    "cha",
    "chha",
    "xa",
}


@dataclass(frozen=True)
class PersonalDateScope:
    label: str
    start: date
    end: date


@dataclass(frozen=True)
class PersonalToolContext:
    route_label: str
    mode: str
    warnings: list[str]
    direct_reply: str | None
    evidence: dict | None


def _today() -> date:
    return datetime.utcnow().date()


def _parse_date_scope(query: str) -> PersonalDateScope | None:
    normalized = normalize_neplish_text(query)
    now = _today()

    if "this month" in normalized:
        start = date(now.year, now.month, 1)
        if now.month == 12:
            end = date(now.year + 1, 1, 1) - timedelta(days=1)
        else:
            end = date(now.year, now.month + 1, 1) - timedelta(days=1)
        return PersonalDateScope(label="this month", start=start, end=end)

    if "last month" in normalized or "previous month" in normalized:
        if now.month == 1:
            start = date(now.year - 1, 12, 1)
            end = date(now.year, 1, 1) - timedelta(days=1)
        else:
            start = date(now.year, now.month - 1, 1)
            end = date(now.year, now.month, 1) - timedelta(days=1)
        return PersonalDateScope(label="last month", start=start, end=end)

    if "this year" in normalized:
        return PersonalDateScope(label="this year", start=date(now.year, 1, 1), end=date(now.year, 12, 31))

    if "last year" in normalized or "previous year" in normalized:
        return PersonalDateScope(
            label="last year",
            start=date(now.year - 1, 1, 1),
            end=date(now.year - 1, 12, 31),
        )

    if "today" in normalized:
        return PersonalDateScope(label="today", start=now, end=now)

    if "yesterday" in normalized:
        yday = now - timedelta(days=1)
        return PersonalDateScope(label="yesterday", start=yday, end=yday)

    match = re.search(r"last\s+(\d{1,3})\s+days", normalized)
    if match:
        days = max(1, min(365, int(match.group(1))))
        start = now - timedelta(days=days - 1)
        return PersonalDateScope(label=f"last {days} days", start=start, end=now)

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
    scope: PersonalDateScope | None,
    limit: int = 500,
) -> list[dict]:
    where_parts = [
        "tfv.user_id = %(user_id)s::uuid",
        "tfv.txn_type in ('income', 'expense', 'transfer', 'loan_out', 'loan_in', 'repayment_in', 'repayment_out')",
    ]
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


def _normalize_match_text(value: object) -> str:
    normalized = normalize_neplish_text(str(value or ""))
    normalized = re.sub(r"[^a-z0-9\s]", " ", normalized.lower())
    return re.sub(r"\s+", " ", normalized).strip()


def _extract_personal_query_candidates(query: str) -> list[str]:
    normalized = _normalize_match_text(query)
    if not normalized:
        return []

    tokens = [
        token
        for token in normalized.split()
        if token and token not in PERSONAL_QUERY_STOPWORDS and not token.isdigit()
    ]
    if not tokens:
        return []

    candidates: list[str] = []
    max_n = min(4, len(tokens))
    for size in range(max_n, 0, -1):
        for idx in range(0, len(tokens) - size + 1):
            candidate = " ".join(tokens[idx : idx + size]).strip()
            if len(candidate) >= 2:
                candidates.append(candidate)

    unique: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        unique.append(candidate)
    return unique


def _score_personal_candidate_match(candidate: str, target: str) -> float:
    candidate_norm = _normalize_match_text(candidate)
    target_norm = _normalize_match_text(target)
    if not candidate_norm or not target_norm:
        return 0.0
    if candidate_norm == target_norm:
        return 1.0
    if len(candidate_norm) >= 3 and candidate_norm in target_norm:
        return 0.95
    if len(target_norm) >= 3 and target_norm in candidate_norm:
        return 0.9
    overlap = len(set(candidate_norm.split()) & set(target_norm.split()))
    ratio = SequenceMatcher(None, candidate_norm, target_norm).ratio()
    return max(ratio, min(0.89, 0.55 + 0.12 * overlap))


def _build_retrieval_matches(rows: list[dict], query: str, limit: int = 8) -> list[dict]:
    normalized = _normalize_match_text(query)
    candidates = _extract_personal_query_candidates(query)
    if not normalized and not candidates:
        return []

    scored: list[tuple[float, dict]] = []
    for row in rows:
        description = str(row.get("description") or "").strip()
        category = str(row.get("category_name") or "").strip()
        counterparty_name = str(row.get("counterparty_name") or "").strip()
        haystack = _normalize_match_text(f"{description} {category} {counterparty_name}")

        score = 0.0
        if normalized and normalized in haystack:
            score = max(score, 0.82)
        for candidate in candidates:
            score = max(score, _score_personal_candidate_match(candidate, counterparty_name))
            score = max(score, 0.74 if candidate and candidate in haystack else 0.0)

        if score >= 0.72:
            scored.append(
                (
                    score,
                    {
                        "txn_type": row.get("txn_type"),
                        "amount": float(row.get("amount") or 0),
                        "date": row.get("date"),
                        "category": row.get("category_name"),
                        "description": row.get("description"),
                        "counterparty_name": counterparty_name or None,
                        "match_confidence": round(score, 3),
                    },
                )
            )

    scored.sort(
        key=lambda item: (
            item[0],
            str(item[1].get("date") or ""),
            float(item[1].get("amount") or 0),
        ),
        reverse=True,
    )
    return [item[1] for item in scored[: max(1, min(limit, 20))]]


def _fetch_counterparty_positions(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str | None,
) -> list[dict]:
    if not profile_id:
        return []

    rows = list_counterparty_positions(conn, user_id=user_id, profile_id=profile_id)
    normalized_rows: list[dict] = []
    for row in rows:
        normalized_rows.append(
            {
                "counterparty_id": row.get("counterparty_id"),
                "name": str(row.get("name") or "").strip(),
                "relation_type": str(row.get("relation_type") or "").strip(),
                "opening_balance": float(row.get("opening_balance") or 0),
                "receivable": float(row.get("receivable") or 0),
                "payable": float(row.get("payable") or 0),
                "net_position": float(row.get("net_position") or 0),
            }
        )
    return normalized_rows


def _build_counterparty_position_matches(positions: list[dict], query: str, limit: int = 8) -> list[dict]:
    normalized = _normalize_match_text(query)
    candidates = _extract_personal_query_candidates(query)
    if not normalized and not candidates:
        return []

    scored: list[tuple[float, dict]] = []
    for row in positions:
        name = str(row.get("name") or "").strip()
        relation_type = str(row.get("relation_type") or "").strip()
        haystack = _normalize_match_text(f"{name} {relation_type}")

        score = 0.0
        if normalized and normalized in haystack:
            score = max(score, 0.82)
        for candidate in candidates:
            score = max(score, _score_personal_candidate_match(candidate, name))
            score = max(score, 0.74 if candidate and candidate in haystack else 0.0)

        if score >= 0.72:
            enriched = dict(row)
            enriched["match_confidence"] = round(score, 3)
            scored.append((score, enriched))

    scored.sort(
        key=lambda item: (
            item[0],
            abs(float(item[1].get("net_position") or 0)),
            abs(float(item[1].get("receivable") or 0)) + abs(float(item[1].get("payable") or 0)),
        ),
        reverse=True,
    )
    return [item[1] for item in scored[: max(1, min(limit, 20))]]


def _build_recent_transactions(rows: list[dict], limit: int = 12) -> list[dict]:
    recent: list[dict] = []
    for row in rows[: max(1, min(limit, 20))]:
        recent.append(
            {
                "txn_type": row.get("txn_type"),
                "amount": round(float(row.get("amount") or 0), 2),
                "date": row.get("date"),
                "category": row.get("category_name"),
                "description": row.get("description"),
                "account_name": row.get("account_name"),
                "counterparty_name": row.get("counterparty_name"),
            }
        )
    return recent


def _is_personal_inflow_txn(txn_type: object) -> bool:
    return str(txn_type or "").strip().lower() in {"income", "loan_in", "repayment_in"}


def _is_personal_outflow_txn(txn_type: object) -> bool:
    return str(txn_type or "").strip().lower() in {"expense", "loan_out", "repayment_out"}


def _money(value: object) -> str:
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        amount = 0.0
    return f"NPR {amount:,.2f}"


def _personal_phrase(mode: str, *, en: str, np: str, ne: str | None = None) -> str:
    if mode == "nepali":
        return ne or np
    if mode == "neplish":
        return np
    return en


def _personal_no_data_message(mode: str) -> str:
    return _personal_phrase(
        mode,
        en="I do not have enough personal finance data yet. Add a few transactions, then ask again.",
        np="M sanga ahile personal finance ko pugne data chaina. Kehi transactions add garnuhos, ani feri sodhnuhos.",
        ne="मसँग अहिले पर्याप्त व्यक्तिगत वित्तीय डेटा छैन। केही कारोबार थपेर फेरि सोध्नुहोस्।",
    )


def _build_personal_transaction_history_reply(
    rows: list[dict],
    *,
    query: str,
    scope: PersonalDateScope | None,
    response_language_mode: str,
    limit: int = 8,
) -> str | None:
    if not PERSONAL_TRANSACTION_HISTORY_PATTERN.search(str(query or "")):
        return None

    relevant_rows = rows[: max(1, min(limit, 20))]
    scope_label = scope.label if scope else "all time"
    if not rows:
        return (
            f"{_personal_phrase(response_language_mode, en=f'Personal transaction history for {scope_label}:', np=f'Personal transaction history for {scope_label}:', ne=f'{scope_label} को व्यक्तिगत कारोबार इतिहास:')}\n"
            f"{_personal_phrase(response_language_mode, en='- No transactions were found for that period.', np='- Tyo period ma kunai transaction bhetiyena.', ne='- त्यो अवधिमा कुनै पनि कारोबार भेटिएन।')}\n"
            f"{_personal_phrase(response_language_mode, en=f'Summary: As of {date.today().isoformat()}.', np=f'Summary: {date.today().isoformat()} samma.', ne=f'सारांश: {date.today().isoformat()} सम्म।')}"
        )

    income_rows = [row for row in rows if str(row.get("txn_type") or "").strip() == "income"]
    expense_rows = [row for row in rows if str(row.get("txn_type") or "").strip() == "expense"]
    first_date = str(rows[-1].get("date") or "").strip() or "Not available"
    last_date = str(rows[0].get("date") or "").strip() or "Not available"
    total_income = sum(float(row.get("amount") or 0) for row in income_rows)
    total_expense = sum(float(row.get("amount") or 0) for row in expense_rows)

    lines = [
        _personal_phrase(
            response_language_mode,
            en=f"Personal transaction history for {scope_label}:",
            np=f"Personal transaction history for {scope_label}:",
            ne=f"{scope_label} को व्यक्तिगत कारोबार इतिहास:",
        ),
        _personal_phrase(response_language_mode, en=f"- Transactions recorded: {len(rows)}", np=f"- Transactions recorded: {len(rows)}", ne=f"- रेकर्ड भएका कारोबार: {len(rows)}"),
        _personal_phrase(response_language_mode, en=f"- Income total: {_money(total_income)}", np=f"- Income total: {_money(total_income)}", ne=f"- कुल आम्दानी: {_money(total_income)}"),
        _personal_phrase(response_language_mode, en=f"- Expense total: {_money(total_expense)}", np=f"- Expense total: {_money(total_expense)}", ne=f"- कुल खर्च: {_money(total_expense)}"),
        _personal_phrase(response_language_mode, en=f"- Net movement: {_money(total_income - total_expense)}", np=f"- Net movement: {_money(total_income - total_expense)}", ne=f"- खुद आवागमन: {_money(total_income - total_expense)}"),
        _personal_phrase(response_language_mode, en=f"- Period covered by matching entries: {first_date} to {last_date}", np=f"- Matching entries cover: {first_date} to {last_date}", ne=f"- मिलेका कारोबारको अवधि: {first_date} देखि {last_date} सम्म"),
        _personal_phrase(response_language_mode, en=f"Recent transactions ({scope_label}, latest {len(relevant_rows)}):", np=f"Recent transactions ({scope_label}, latest {len(relevant_rows)}):", ne=f"हालका कारोबारहरू ({scope_label}, पछिल्ला {len(relevant_rows)}):"),
    ]

    for row in relevant_rows:
        tx_date = str(row.get("date") or "").strip() or "-"
        txn_type = str(row.get("txn_type") or "entry").replace("_", " ").title()
        amount = _money(row.get("amount"))
        category = str(row.get("category_name") or "").strip() or "Uncategorized"
        description = str(row.get("description") or "").strip()
        counterparty = str(row.get("counterparty_name") or "").strip()
        parts = [tx_date, txn_type, amount, category]
        if counterparty:
            parts.append(counterparty)
        if description:
            parts.append(description)
        lines.append(f"- {' | '.join(parts)}")

    lines.append(
        _personal_phrase(
            response_language_mode,
            en=f"Summary: As of {date.today().isoformat()}.",
            np=f"Summary: {date.today().isoformat()} samma.",
            ne=f"सारांश: {date.today().isoformat()} सम्म।",
        )
    )
    return "\n".join(lines)


def _personal_counterparty_flags(query: str) -> dict[str, bool]:
    normalized_query = _normalize_match_text(query)
    return {
        "asks_about_receiving": any(token in normalized_query for token in {"received", "receive", "from", "got"}),
        "asks_about_paying": any(
            token in normalized_query
            for token in {
                "paid",
                "pay",
                "spend",
                "spent",
                "give",
                "gave",
                "owe",
                "tirnu",
                "tirna",
                "dinu",
                "baki",
                "borrow",
                "borrowed",
                "loan",
                "udhar",
                "rin",
                "sapati",
            }
        ),
        "asks_should_pay": any(
            token in normalized_query
            for token in {
                "should i pay",
                "owe",
                "owed",
                "tirnu",
                "tirna",
                "baki",
                "dinu cha",
                "tirnu cha",
                "borrow",
                "borrowed",
                "loan",
                "udhar",
                "rin",
                "sapati",
            }
        ),
        "asks_about_lending": any(
            token in normalized_query
            for token in {
                "lend",
                "lent",
                "loaned",
                "receivable",
                "receive back",
                "linu",
                "linu cha",
            }
        ),
        "asks_about_borrowing": any(
            token in normalized_query
            for token in {"borrow", "borrowed", "loan", "payable", "udhar", "rin", "sapati", "tirnu", "linu"}
        ),
        "asks_for_people": any(
            token in normalized_query
            for token in {
                "who",
                "which",
                "people",
                "persons",
                "person",
                "names",
                "name",
                "kas",
                "ko ko",
            }
        ),
    }


def _build_personal_counterparty_position_reply(
    query: str,
    matched_positions: list[dict],
    *,
    response_language_mode: str,
) -> str | None:
    if not PERSONAL_COUNTERPARTY_QUERY_PATTERN.search(str(query or "")):
        return None
    if not matched_positions:
        return None

    flags = _personal_counterparty_flags(query)
    asks_for_people = flags["asks_for_people"]
    asks_about_lending = flags["asks_about_lending"]
    asks_about_borrowing = flags["asks_about_borrowing"] or flags["asks_should_pay"]

    if asks_for_people:
        if asks_about_lending and not asks_about_borrowing:
            relevant_positions = [
                row for row in matched_positions if float(row.get("receivable") or 0) > 0
            ]
            if not relevant_positions:
                return "\n".join(
                    [
                        _personal_phrase(
                            response_language_mode,
                            en="People you have lent to:",
                            np="Tapai le lend gareko manche haru:",
                            ne="तपाईंले सापटी दिएको व्यक्तिहरू:",
                        ),
                        _personal_phrase(
                            response_language_mode,
                            en="- No receivable personal balances are recorded right now.",
                            np="- Ahile kunai receivable personal balance record bhayeko chaina.",
                            ne="- अहिले कुनै प्राप्त गर्न बाँकी व्यक्तिगत रकम रेकर्ड भएको छैन।",
                        ),
                    ]
                )
            lines = [
                _personal_phrase(
                    response_language_mode,
                    en="People you have lent to:",
                    np="Tapai le lend gareko manche haru:",
                    ne="तपाईंले सापटी दिएको व्यक्तिहरू:",
                )
            ]
            for row in relevant_positions[:8]:
                lines.append(f"- {row.get('name')}: receivable {_money(row.get('receivable'))}")
            lines.append(
                _personal_phrase(
                    response_language_mode,
                    en=f"Summary: As of {date.today().isoformat()}.",
                    np=f"Summary: {date.today().isoformat()} samma.",
                    ne=f"सारांश: {date.today().isoformat()} सम्म।",
                )
            )
            return "\n".join(lines)

        if asks_about_borrowing and not asks_about_lending:
            relevant_positions = [row for row in matched_positions if float(row.get("payable") or 0) > 0]
            if not relevant_positions:
                return "\n".join(
                    [
                        _personal_phrase(
                            response_language_mode,
                            en="People you have borrowed from:",
                            np="Tapai le borrow gareko manche haru:",
                            ne="तपाईंले सापटी लिएको व्यक्तिहरू:",
                        ),
                        _personal_phrase(
                            response_language_mode,
                            en="- No payable personal balances are recorded right now.",
                            np="- Ahile kunai payable personal balance record bhayeko chaina.",
                            ne="- अहिले कुनै तिर्न बाँकी व्यक्तिगत रकम रेकर्ड भएको छैन।",
                        ),
                    ]
                )
            lines = [
                _personal_phrase(
                    response_language_mode,
                    en="People you have borrowed from:",
                    np="Tapai le borrow gareko manche haru:",
                    ne="तपाईंले सापटी लिएको व्यक्तिहरू:",
                )
            ]
            for row in relevant_positions[:8]:
                lines.append(f"- {row.get('name')}: payable {_money(row.get('payable'))}")
            lines.append(
                _personal_phrase(
                    response_language_mode,
                    en=f"Summary: As of {date.today().isoformat()}.",
                    np=f"Summary: {date.today().isoformat()} samma.",
                    ne=f"सारांश: {date.today().isoformat()} सम्म।",
                )
            )
            return "\n".join(lines)

    row = matched_positions[0]
    counterparty_name = str(row.get("name") or "").strip() or "this counterparty"
    receivable = float(row.get("receivable") or 0)
    payable = float(row.get("payable") or 0)
    net_position = float(row.get("net_position") or 0)

    lines = [
        _personal_phrase(
            response_language_mode,
            en=f"Position with {counterparty_name}:",
            np=f"{counterparty_name} sanga ko position:",
            ne=f"{counterparty_name} सँगको स्थिति:",
        )
    ]

    if asks_about_lending and not asks_about_borrowing:
        if receivable > 0:
            lines.append(
                _personal_phrase(
                    response_language_mode,
                    en=f"{counterparty_name} owes you {_money(receivable)} based on your recorded personal counterparty balance.",
                    np=f"Recorded personal counterparty balance anusar, {counterparty_name} le tapai lai {_money(receivable)} tirna baki cha.",
                    ne=f"रेकर्ड भएका व्यक्तिगत हिसाबअनुसार, {counterparty_name} ले तपाईंलाई {_money(receivable)} तिर्न बाँकी छ।",
                )
            )
        else:
            lines.append(
                _personal_phrase(
                    response_language_mode,
                    en=f"You do not currently have a receivable balance from {counterparty_name}.",
                    np=f"Ahile {counterparty_name} bata tapai ko receivable balance chaina.",
                    ne=f"अहिले {counterparty_name} बाट तपाईंको प्राप्त गर्न बाँकी रकम छैन।",
                )
            )
    elif asks_about_borrowing:
        if payable > 0:
            lines.append(
                _personal_phrase(
                    response_language_mode,
                    en=f"You owe {counterparty_name} {_money(payable)} based on your recorded personal counterparty balance.",
                    np=f"Recorded personal counterparty balance anusar, tapai le {counterparty_name} lai {_money(payable)} tirna baki cha.",
                    ne=f"रेकर्ड भएका व्यक्तिगत हिसाबअनुसार, तपाईंले {counterparty_name} लाई {_money(payable)} तिर्न बाँकी छ।",
                )
            )
        else:
            lines.append(
                _personal_phrase(
                    response_language_mode,
                    en=f"You do not currently have a payable balance to {counterparty_name}.",
                    np=f"Ahile {counterparty_name} lai tapai ko payable balance chaina.",
                    ne=f"अहिले {counterparty_name} लाई तपाईंको तिर्न बाँकी रकम छैन।",
                )
            )
    else:
        if payable > 0 and receivable <= 0:
            lines.append(
                _personal_phrase(
                    response_language_mode,
                    en=f"You currently owe {counterparty_name} {_money(payable)}.",
                    np=f"Ahile tapai le {counterparty_name} lai {_money(payable)} tirna baki cha.",
                    ne=f"अहिले तपाईंले {counterparty_name} लाई {_money(payable)} तिर्न बाँकी छ।",
                )
            )
        elif receivable > 0 and payable <= 0:
            lines.append(
                _personal_phrase(
                    response_language_mode,
                    en=f"{counterparty_name} currently owes you {_money(receivable)}.",
                    np=f"Ahile {counterparty_name} le tapai lai {_money(receivable)} tirna baki cha.",
                    ne=f"अहिले {counterparty_name} ले तपाईंलाई {_money(receivable)} तिर्न बाँकी छ।",
                )
            )
        else:
            lines.append(
                _personal_phrase(
                    response_language_mode,
                    en=f"Your recorded balance with {counterparty_name} includes both receivable and payable activity.",
                    np=f"{counterparty_name} sanga ko recorded balance ma receivable ra payable dubai activity cha.",
                    ne=f"{counterparty_name} सँगको रेकर्ड गरिएको हिसाबमा प्राप्ति र भुक्तानी दुवै गतिविधि छन्।",
                )
            )

    lines.append(f"- Receivable: {_money(receivable)}")
    lines.append(f"- Payable: {_money(payable)}")
    lines.append(f"- Net position: {_money(net_position)}")
    lines.append(
        _personal_phrase(
            response_language_mode,
            en=f"Summary: As of {date.today().isoformat()}.",
            np=f"Summary: {date.today().isoformat()} samma.",
            ne=f"सारांश: {date.today().isoformat()} सम्म।",
        )
    )
    return "\n".join(lines)


def _build_personal_counterparty_reply(
    query: str,
    matched_rows: list[dict],
    *,
    response_language_mode: str,
) -> str | None:
    if not PERSONAL_COUNTERPARTY_QUERY_PATTERN.search(str(query or "")):
        return None
    if not matched_rows:
        return None

    counterparty_name = str(matched_rows[0].get("counterparty_name") or "").strip() or "this counterparty"
    expense_rows = [row for row in matched_rows if _is_personal_outflow_txn(row.get("txn_type"))]
    income_rows = [row for row in matched_rows if _is_personal_inflow_txn(row.get("txn_type"))]

    total_paid = sum(float(row.get("amount") or 0) for row in expense_rows)
    total_received = sum(float(row.get("amount") or 0) for row in income_rows)
    net_outflow = total_paid - total_received
    ordered_rows = sorted(matched_rows, key=lambda row: str(row.get("date") or ""))
    first_date = str(ordered_rows[0].get("date") or "").strip() or "Not available"
    last_date = str(ordered_rows[-1].get("date") or "").strip() or "Not available"

    flags = _personal_counterparty_flags(query)
    asks_about_receiving = flags["asks_about_receiving"]
    asks_about_paying = flags["asks_about_paying"]
    asks_should_pay = flags["asks_should_pay"]
    asks_about_lending = flags["asks_about_lending"]
    asks_about_borrowing = flags["asks_about_borrowing"]
    net_position = total_received - total_paid

    lines = [
        _personal_phrase(
            response_language_mode,
            en=f"Transactions with {counterparty_name}:",
            np=f"{counterparty_name} sanga ko transactions:",
            ne=f"{counterparty_name} सँग भएका कारोबारहरू:",
        )
    ]

    if asks_about_receiving and not asks_about_paying and not asks_about_borrowing:
        lines.append(
            _personal_phrase(
                response_language_mode,
                en=f"You have recorded {_money(total_received)} received from {counterparty_name} across {len(income_rows)} inflow transactions.",
                np=f"Tapai le {counterparty_name} bata {len(income_rows)} inflow transaction ma {_money(total_received)} receive gareko record cha.",
                ne=f"तपाईंले {counterparty_name} बाट {len(income_rows)} वटा प्राप्ति कारोबारमा {_money(total_received)} प्राप्त गरेको रेकर्ड छ।",
            )
        )
    elif asks_should_pay or asks_about_borrowing or asks_about_lending:
        if net_position > 0:
            lines.append(
                _personal_phrase(
                    response_language_mode,
                    en=f"Based on your recorded transactions, you have received {_money(net_position)} more from {counterparty_name} than you have paid.",
                    np=f"Tapai ko recorded transaction anusar, tapai le {counterparty_name} bata tirnu bhanda {_money(net_position)} dherai receive gareko dekhincha.",
                    ne=f"तपाईंका रेकर्ड भएका कारोबारअनुसार, तपाईंले {counterparty_name} बाट तिरेकोभन्दा {_money(net_position)} बढी प्राप्त गरेको देखिन्छ।",
                )
            )
        elif net_position < 0:
            lines.append(
                _personal_phrase(
                    response_language_mode,
                    en=f"Based on your recorded transactions, you have paid {_money(abs(net_position))} more to {counterparty_name} than you have received.",
                    np=f"Tapai ko recorded transaction anusar, tapai le {counterparty_name} lai receive gareko bhanda {_money(abs(net_position))} dherai tireko dekhincha.",
                    ne=f"तपाईंका रेकर्ड भएका कारोबारअनुसार, तपाईंले {counterparty_name} लाई प्राप्त गरेकोभन्दा {_money(abs(net_position))} बढी तिरेको देखिन्छ।",
                )
            )
        else:
            lines.append(
                _personal_phrase(
                    response_language_mode,
                    en=f"Based on your recorded transactions with {counterparty_name}, your paid and received amounts are balanced.",
                    np=f"{counterparty_name} sanga ko recorded transaction anusar, tapai ko paid ra received amount barabar cha.",
                    ne=f"{counterparty_name} सँगका रेकर्ड भएका कारोबारअनुसार, तपाईंको तिरेको र प्राप्त गरेको रकम बराबर छ।",
                )
            )
    else:
        lines.append(
            _personal_phrase(
                response_language_mode,
                en=f"You have recorded {_money(total_paid)} paid to {counterparty_name} across {len(expense_rows)} outflow transactions.",
                np=f"Tapai le {counterparty_name} lai {len(expense_rows)} outflow transaction ma {_money(total_paid)} pay gareko record cha.",
                ne=f"तपाईंले {counterparty_name} लाई {len(expense_rows)} वटा भुक्तानी कारोबारमा {_money(total_paid)} तिरेको रेकर्ड छ।",
            )
        )

    if total_received > 0:
        lines.append(f"- Total received from {counterparty_name}: {_money(total_received)}")
    if total_paid > 0:
        lines.append(f"- Total paid to {counterparty_name}: {_money(total_paid)}")
    lines.append(f"- Net cash movement: {_money(net_outflow)}")
    lines.append(f"- Period covered by matching entries: {first_date} to {last_date}")

    for row in matched_rows[:5]:
        tx_date = str(row.get("date") or "").strip() or "-"
        txn_type = str(row.get("txn_type") or "entry").replace("_", " ").title()
        amount = _money(row.get("amount"))
        category = str(row.get("category") or row.get("category_name") or "").strip() or "Uncategorized"
        description = str(row.get("description") or "").strip()
        detail = f"- {tx_date} | {txn_type} | {amount} | {category}"
        if description:
            detail += f" | {description}"
        lines.append(detail)

    if asks_should_pay:
        lines.append(
            _personal_phrase(
                response_language_mode,
                en="Keep in mind: This answer reflects your recorded transaction history with this person. If there is an unpaid amount outside the app records, I can only confirm what is already recorded.",
                np="Keep in mind: Yo answer tapai ko yo manche sanga bhayeko recorded transaction history ma adharit cha. App bahira ko unpaid amount bhaye ma record bhayeko kura matra confirm garna sakchu.",
                ne="ध्यान दिनुहोस्: यो उत्तर यस व्यक्तिसँग भएका तपाईंका रेकर्ड भएका कारोबारमा आधारित छ। एपबाहिरको कुनै बाँकी रकम भए, म रेकर्ड भएको कुरामात्र पुष्टि गर्न सक्छु।",
            )
        )

    lines.append(
        _personal_phrase(
            response_language_mode,
            en=f"Summary: As of {date.today().isoformat()}.",
            np=f"Summary: {date.today().isoformat()} samma.",
            ne=f"सारांश: {date.today().isoformat()} सम्म।",
        )
    )
    return "\n".join(lines)


def _build_personal_category_breakdown_reply(
    query: str,
    *,
    finance_context: dict | None,
    scope: PersonalDateScope | None,
    response_language_mode: str,
) -> str | None:
    understanding = parse_personal_query_understanding(query)
    if understanding.intent != "category_breakdown":
        return None
    if not isinstance(finance_context, dict):
        return None

    category_breakdown = finance_context.get("category_breakdown")
    if not isinstance(category_breakdown, dict):
        return None

    normalized_query = normalize_neplish_text(query)
    wants_income = bool(re.search(r"\b(income|earn|salary|aamdani|amdani)\b", normalized_query, re.IGNORECASE))
    wants_expense = bool(
        re.search(r"\b(expense|expenses|spend|spending|kharcha|karcha)\b", normalized_query, re.IGNORECASE)
    )
    scope_label = scope.label if scope else "all time"

    income_categories = list(category_breakdown.get("income_by_category") or [])
    expense_categories = list(category_breakdown.get("expense_by_category") or [])

    if wants_income and not wants_expense:
        lines = [
            _personal_phrase(
                response_language_mode,
                en=f"Income categories for {scope_label}:",
                np=f"Income categories for {scope_label}:",
                ne=f"{scope_label} ko income catiegoreies:",
            )
        ]
        if not income_categories:
            lines.append(
                _personal_phrase(
                    response_language_mode,
                    en="- No income category data was found for that period.",
                    np="- Tyo period ko income category data bhetiyena.",
                    ne="- Tyo period ko income category data bhetiyena.",
                )
            )
        else:
            for row in income_categories[:6]:
                name = str(row.get("category") or "Uncategorized").strip() or "Uncategorized"
                amount = _money(row.get("amount"))
                pct = row.get("pct_of_total")
                pct_label = f" ({round(float(pct) * 100, 1)}%)" if pct is not None else ""
                lines.append(f"- {name}: {amount}{pct_label}")
        lines.append(
            _personal_phrase(
                response_language_mode,
                en=f"Summary: As of {date.today().isoformat()}.",
                np=f"Summary: {date.today().isoformat()} samma.",
                ne=f"Saransh: {date.today().isoformat()} samma.",
            )
        )
        return "\n".join(lines)

    if wants_expense or (expense_categories and not income_categories):
        lines = [
            _personal_phrase(
                response_language_mode,
                en=f"Expense categories for {scope_label}:",
                np=f"Expense categories for {scope_label}:",
                ne=f"{scope_label} ko expense catiegoreies:",
            )
        ]
        if not expense_categories:
            lines.append(
                _personal_phrase(
                    response_language_mode,
                    en="- No expense category data was found for that period.",
                    np="- Tyo period ko expense category data bhetiyena.",
                    ne="- Tyo period ko expense category data bhetiyena.",
                )
            )
        else:
            for row in expense_categories[:6]:
                name = str(row.get("category") or "Uncategorized").strip() or "Uncategorized"
                amount = _money(row.get("amount"))
                pct = row.get("pct_of_total")
                pct_label = f" ({round(float(pct) * 100, 1)}%)" if pct is not None else ""
                lines.append(f"- {name}: {amount}{pct_label}")
        lines.append(
            _personal_phrase(
                response_language_mode,
                en=f"Summary: As of {date.today().isoformat()}.",
                np=f"Summary: {date.today().isoformat()} samma.",
                ne=f"Saransh: {date.today().isoformat()} samma.",
            )
        )
        return "\n".join(lines)

    lines = [
        _personal_phrase(
            response_language_mode,
            en=f"Category breakdown for {scope_label}:",
            np=f"Category breakdown for {scope_label}:",
            ne=f"{scope_label} ko catiegory breakdown:",
        )
    ]
    if expense_categories:
        lines.append("Expense categories:")
        for row in expense_categories[:5]:
            name = str(row.get("category") or "Uncategorized").strip() or "Uncategorized"
            amount = _money(row.get("amount"))
            pct = row.get("pct_of_total")
            pct_label = f" ({round(float(pct) * 100, 1)}%)" if pct is not None else ""
            lines.append(f"- {name}: {amount}{pct_label}")
    if income_categories:
        lines.append("Income categories:")
        for row in income_categories[:5]:
            name = str(row.get("category") or "Uncategorized").strip() or "Uncategorized"
            amount = _money(row.get("amount"))
            pct = row.get("pct_of_total")
            pct_label = f" ({round(float(pct) * 100, 1)}%)" if pct is not None else ""
            lines.append(f"- {name}: {amount}{pct_label}")
    if not income_categories and not expense_categories:
        lines.append(
            _personal_phrase(
                response_language_mode,
                en="- No category data was found for that period.",
                np="- Tyo period ko category data bhetiyena.",
                ne="- Tyo period ko category data bhetiyena.",
            )
        )
    lines.append(
        _personal_phrase(
            response_language_mode,
            en=f"Summary: As of {date.today().isoformat()}.",
            np=f"Summary: {date.today().isoformat()} samma.",
            ne=f"Saransh: {date.today().isoformat()} samma.",
        )
    )
    return "\n".join(lines)


def build_personal_tool_context(
    conn: Connection,
    *,
    settings: Settings,
    user_id: str,
    user_query: str,
) -> PersonalToolContext:
    warnings: list[str] = []
    query = str(user_query or "").strip()
    normalized_query = normalize_neplish_text(query)
    response_language_mode = detect_response_language_mode(query)
    route = route_personal_chat_intent(query)

    if is_business_question_for_personal_chat(query):
        return PersonalToolContext(
            route_label=route.label,
            mode=route.mode,
            warnings=warnings,
            direct_reply=format_personal_reply_like_business_structure(get_personal_to_business_handoff_message()),
            evidence=None,
        )

    if SIMPLE_GREETING_PATTERN.match(query):
        return PersonalToolContext(
            route_label=route.label,
            mode=route.mode,
            warnings=warnings,
            direct_reply=format_personal_reply_like_business_structure(
                _personal_phrase(
                    response_language_mode,
                    en="Hello! I can help with your spending insights, budgets, and personal summaries.",
                    np="Hello! Ma tapai lai spending insights, budget, ra personal summary ma help garna sakchu.",
                    ne="नमस्ते! म तपाईंलाई खर्च सम्बन्धी जानकारी, बजेट र व्यक्तिगत सारांशमा सहयोग गर्न सक्छु।",
                )
            ),
            evidence=None,
        )

    try:
        deterministic = try_generate_deterministic_personal_response(
            conn,
            user_id=user_id,
            user_query=query,
        )
    except Exception:
        warnings.append("Personal deterministic responder failed. Falling back to transaction evidence.")
        deterministic = None
    if deterministic and deterministic.handled and deterministic.reply:
        return PersonalToolContext(
            route_label=deterministic.route,
            mode=route.mode,
            warnings=warnings,
            direct_reply=format_personal_reply_like_business_structure(deterministic.reply),
            evidence=None,
        )

    scope = _parse_date_scope(normalized_query or query)
    try:
        profile_id = _get_personal_profile_id(conn, user_id)
    except Exception:
        warnings.append("Could not load personal profile metadata.")
        profile_id = None
    if not profile_id:
        warnings.append("No personal profile found.")

    try:
        rows = _fetch_transactions(conn, user_id=user_id, profile_id=profile_id, scope=scope, limit=500)
    except Exception:
        warnings.append("Could not load personal transaction history right now.")
        rows = []
    try:
        counterparty_positions = _fetch_counterparty_positions(
            conn,
            user_id=user_id,
            profile_id=profile_id,
        )
    except Exception:
        warnings.append("Could not load personal counterparty balances right now.")
        counterparty_positions = []

    matched_transactions = _build_retrieval_matches(rows, normalized_query or query, limit=8)
    matched_positions = _build_counterparty_position_matches(counterparty_positions, normalized_query or query, limit=8)
    counterparty_flags = _personal_counterparty_flags(normalized_query or query)
    relevant_counterparty_positions = (
        counterparty_positions if counterparty_flags["asks_for_people"] and counterparty_positions else matched_positions
    )

    direct_history_reply = _build_personal_transaction_history_reply(
        rows,
        query=normalized_query or query,
        scope=scope,
        response_language_mode=response_language_mode,
    )
    if direct_history_reply:
        return PersonalToolContext(
            route_label=route.label,
            mode=route.mode,
            warnings=warnings,
            direct_reply=format_personal_reply_like_business_structure(direct_history_reply),
            evidence=None,
        )
    direct_counterparty_position_reply = _build_personal_counterparty_position_reply(
        normalized_query or query,
        relevant_counterparty_positions,
        response_language_mode=response_language_mode,
    )
    if direct_counterparty_position_reply:
        return PersonalToolContext(
            route_label=route.label,
            mode=route.mode,
            warnings=warnings,
            direct_reply=format_personal_reply_like_business_structure(direct_counterparty_position_reply),
            evidence=None,
        )
    direct_counterparty_reply = _build_personal_counterparty_reply(
        normalized_query or query,
        matched_transactions,
        response_language_mode=response_language_mode,
    )
    if direct_counterparty_reply:
        return PersonalToolContext(
            route_label=route.label,
            mode=route.mode,
            warnings=warnings,
            direct_reply=format_personal_reply_like_business_structure(direct_counterparty_reply),
            evidence=None,
        )
    if route.mode == "finance" and not rows:
        return PersonalToolContext(
            route_label=route.label,
            mode=route.mode,
            warnings=warnings,
            direct_reply=format_personal_reply_like_business_structure(
                _personal_no_data_message(response_language_mode)
            ),
            evidence=None,
        )

    total_income = sum(float(row.get("amount") or 0) for row in rows if row.get("txn_type") == "income")
    total_expense = sum(float(row.get("amount") or 0) for row in rows if row.get("txn_type") == "expense")
    balance = total_income - total_expense

    # ── build structured personal finance context ─────────────────────────
    # This is the personal equivalent of accounting_statement_service output.
    # It gives the LLM structured income statement, cash position, savings rate,
    # period comparison, category vertical %, and top counterparties.
    try:
        finance_context = build_personal_finance_context(
            conn,
            user_id=user_id,
            profile_id=profile_id,
            start_date=scope.start if scope else None,
            end_date=scope.end if scope else None,
            query=normalized_query or query,
        )
    except Exception:
        warnings.append("Personal finance context builder failed. Evidence will use raw transaction totals only.")
        finance_context = None

    personal_vector_matches: list[dict] = []
    if profile_id:
        try:
            vector_context = get_personal_vector_context(
                conn,
                settings=settings,
                user_id=user_id,
                profile_id=profile_id,
                query=normalized_query or query,
            )
            warnings.extend(vector_context.warnings)
            personal_vector_matches = vector_context.matches
        except Exception:
            warnings.append("Personal semantic retrieval is unavailable right now. Answers will use SQL evidence.")
            personal_vector_matches = []

    evidence = {
        "mode": route.mode,
        "normalized_query": normalized_query or query.lower(),
        "response_language_mode": response_language_mode,
        "query_candidates": _extract_personal_query_candidates(normalized_query or query),
        "scope": {
            "label": scope.label,
            "start": scope.start.isoformat(),
            "end": scope.end.isoformat(),
        }
        if scope
        else None,
        # legacy flat summary — kept for backward compatibility with existing prompt rules
        "summary": {
            "transactions_count": len(rows),
            "total_income": round(total_income, 2),
            "total_expense": round(total_expense, 2),
            "net_balance": round(balance, 2),
        },
        # structured context — primary source of truth (mirrors accounting_context in business)
        # contains: income_statement, cash_position, ratios, period_comparison,
        #           category_breakdown, top_counterparties, notes
        "finance_context": finance_context,
        "top_expense_categories": [
            {"category": name, "amount": round(amount, 2)}
            for name, amount in _build_top_categories(rows, "expense", top_n=5)
        ],
        "top_income_categories": [
            {"category": name, "amount": round(amount, 2)}
            for name, amount in _build_top_categories(rows, "income", top_n=5)
        ],
        "recent_transactions": _build_recent_transactions(rows, limit=12),
        "matched_transactions": matched_transactions,
        "counterparty_positions": counterparty_positions,
        "matched_counterparty_positions": matched_positions,
        "retrieved_personal_facts": [
            {
                "source_kind": row.get("source_kind"),
                "source_id": row.get("source_id"),
                "similarity": float(row.get("similarity") or 0),
                "content": str(row.get("content") or "").strip(),
                "metadata": row.get("metadata") if isinstance(row.get("metadata"), dict) else None,
            }
            for row in personal_vector_matches[:12]
        ],
    }

    direct_category_reply = _build_personal_category_breakdown_reply(
        normalized_query or query,
        finance_context=finance_context,
        scope=scope,
        response_language_mode=response_language_mode,
    )
    if direct_category_reply:
        return PersonalToolContext(
            route_label=route.label,
            mode=route.mode,
            warnings=warnings,
            direct_reply=format_personal_reply_like_business_structure(direct_category_reply),
            evidence=None,
        )

    return PersonalToolContext(
        route_label=route.label,
        mode=route.mode,
        warnings=warnings,
        direct_reply=None,
        evidence=evidence,
    )
