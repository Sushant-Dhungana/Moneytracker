from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal
import re

from psycopg import Connection

from app.arthaxai.chat.neplish import detect_response_language_mode, normalize_neplish_text
from app.repositories.accounts_repository import get_account_balances

PersonalIntent = Literal[
    "balance_lookup",
    "income_summary",
    "expense_summary",
    "savings_summary",
    "category_breakdown",
    "trend_query",
    "counterparty_query",
    "top_expenses",
    "fallback",
]

# ── intent patterns ───────────────────────────────────────────────────────────

# Negative lookahead (?!\s+(to\s+)?(pay|give|owe|return|send|return)) prevents
# "how much do i have to pay sita karki" from matching this pattern.
# Without it, "how much do i have" matches as a substring and the wrong intent fires.
_BALANCE_QUERY_PATTERN = re.compile(
    r"\b("
    r"how much money do i have"
    r"|how much do i have(?!\s+(to\s+)?(pay|give|owe|return|send|give back))"
    r"|my balance|total balance|net balance"
    r"|how much cash do i have|cash balance|bank balance|account balance|money do i have"
    r"|paisa kati cha|balance kati cha|cash kati cha"
    r")\b",
    re.IGNORECASE,
)

# income_summary: user wants to know how much they earned
_INCOME_QUERY_PATTERN = re.compile(
    r"\b(how much (did i |have i )?(earn|earned|receive|received|get|got|income)|"
    r"my income|total income|income (this|last) (month|year|week)|"
    r"aamdani|amdani|talab kati|salary kati|income kati)\b",
    re.IGNORECASE,
)

# expense_summary: user wants to know how much they spent overall
_EXPENSE_QUERY_PATTERN = re.compile(
    r"\b(how much (did i |have i )?(spend|spent|pay|paid|expense)|"
    r"total (expense|expenses|spending)|my expenses|expense (this|last) (month|year|week)|"
    r"kharcha kati|kharcha kati xa|kati kharcha|kharch)\b",
    re.IGNORECASE,
)

# savings_summary: user wants to know how much they saved
_SAVINGS_QUERY_PATTERN = re.compile(
    r"\b(how much (did i |have i )?(save|saved)|my savings|net savings|savings (this|last)|"
    r"bachat kati|kati bachat|net balance|remaining|how much left)\b",
    re.IGNORECASE,
)

# category_breakdown: user wants to see spending by category
_CATEGORY_QUERY_PATTERN = re.compile(
    r"\b(by category|category breakdown|categories|expense categories|income categories|"
    r"spending (by|on)|expense breakdown|income breakdown|"
    r"how much (on|for) (food|rent|transport|shopping|utilities|health|education)|"
    r"category report|top categories|where (am i|did i) (spending|spend))\b",
    re.IGNORECASE,
)

# trend_query: user wants period-over-period comparison
_TREND_QUERY_PATTERN = re.compile(
    r"\b(compare|compared to|vs|versus|last month vs|this month vs|trend|"
    r"more than last|less than last|horizontal analysis|period comparison|"
    r"(this|last) (month|year|week) compare)\b",
    re.IGNORECASE,
)

# counterparty_query: user asks about a specific person/merchant
_COUNTERPARTY_QUERY_PATTERN = re.compile(
    r"\b(how much (did i |have i )?(pay|paid|spend|spent|give|gave) (to )?|"
    r"transactions? (with|from|to)|paid to|received from|"
    r"daraz|esewa|khalti|fonepay|ncell|ntc|nabil|ime|worldlink|"
    r"tirnu|tirna|tiryo|tiris|tirne|tirnu cha|tirna cha|baki cha|dinu cha|linu cha|"
    r"lend|lent|borrow|borrowed|loan|loans|udhar|rin|sapati|receivable|payable)\b",
    re.IGNORECASE,
)

# top_expenses: user wants biggest/largest expenses ranked
_TOP_EXPENSES_PATTERN = re.compile(
    r"\b(top (expense|expenses|spending)|biggest (expense|expenses|spend)|"
    r"largest (expense|expenses)|most (expensive|spent)|what (am i|did i) (spending|spend) most|"
    r"highest expense|highest spending)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PersonalQueryUnderstanding:
    intent: PersonalIntent


@dataclass(frozen=True)
class DeterministicPersonalChatResult:
    handled: bool
    route: Literal["deterministic", "llm_fallback"]
    intent: PersonalIntent
    reply: str | None = None
    resolution_status: Literal["resolved", "fallback"] = "fallback"


def parse_personal_query_understanding(query: str) -> PersonalQueryUnderstanding:
    """
    Classifies the user query into one of 8 personal intents.

    Priority order (most specific → most general):
      1. trend_query        — "compare this month vs last" (must beat summaries)
      2. counterparty_query — "how much did I pay Sita Karki" / "pay to X"
                              MUST be checked BEFORE balance_lookup because
                              "how much do I have to pay X" partially matches
                              the balance pattern even with the negative lookahead.
      3. balance_lookup     — "my balance", "how much do I have" (account balance)
      4. savings_summary    — "how much did I save"
      5. income_summary     — "how much did I earn"
      6. category_breakdown — "spending by category"
      7. top_expenses       — "biggest expenses"
      8. expense_summary    — "how much did I spend" (broad, last resort)
      9. fallback
    """
    normalized = normalize_neplish_text(query)

    # 1. trend_query — comparison / period-over-period
    if _TREND_QUERY_PATTERN.search(normalized):
        return PersonalQueryUnderstanding(intent="trend_query")

    # 2. counterparty_query — specific merchant or person (before balance_lookup)
    #    This catches "how much do I have to pay X" which might still partially
    #    match the balance pattern if the person name is not present.
    if _COUNTERPARTY_QUERY_PATTERN.search(normalized):
        return PersonalQueryUnderstanding(intent="counterparty_query")

    # 3. balance_lookup — account balance question
    #    Negative lookahead in pattern already excludes "to pay/give X" phrasing.
    if _BALANCE_QUERY_PATTERN.search(normalized):
        return PersonalQueryUnderstanding(intent="balance_lookup")

    # 4. savings_summary — net savings / how much left
    if _SAVINGS_QUERY_PATTERN.search(normalized):
        return PersonalQueryUnderstanding(intent="savings_summary")

    # 5. income_summary — how much earned
    if _INCOME_QUERY_PATTERN.search(normalized):
        return PersonalQueryUnderstanding(intent="income_summary")

    # 6. category_breakdown — spending by category
    if _CATEGORY_QUERY_PATTERN.search(normalized):
        return PersonalQueryUnderstanding(intent="category_breakdown")

    # 7. top_expenses — biggest expenses ranked
    if _TOP_EXPENSES_PATTERN.search(normalized):
        return PersonalQueryUnderstanding(intent="top_expenses")

    # 8. expense_summary — general spending (broad, check last before fallback)
    if _EXPENSE_QUERY_PATTERN.search(normalized):
        return PersonalQueryUnderstanding(intent="expense_summary")

    return PersonalQueryUnderstanding(intent="fallback")


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


def _money(value: float) -> str:
    return f"NPR {float(value or 0):,.2f}"


def _phrase(mode: str, *, en: str, np: str, ne: str | None = None) -> str:
    if mode == "nepali":
        return ne or np
    if mode == "neplish":
        return np
    return en


def _personal_balance_reply(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    response_language_mode: str,
) -> str:
    rows = get_account_balances(conn, user_id, profile_id)

    total_balance = 0.0
    cash_and_bank_total = 0.0
    breakdown_lines: list[str] = []
    overdraft_lines: list[str] = []

    for row in rows:
        account_name = str(row.get("account_name") or "Account").strip() or "Account"
        account_type = str(row.get("account_type") or "").strip().lower()
        current_balance = float(row.get("current_balance") or 0)
        total_balance += current_balance

        if account_type in {"cash", "bank"}:
            cash_and_bank_total += current_balance

        line = f"- {account_name}: {_money(current_balance)}"
        if current_balance < 0:
            overdraft_lines.append(line)
        else:
            breakdown_lines.append(line)

    as_of = date.today().isoformat()
    parts = [
        _phrase(
            response_language_mode,
            en="Personal balance:",
            np="Personal balance:",
            ne="व्यक्तिगत ब्यालेन्स:",
        ),
        _phrase(
            response_language_mode,
            en=f"You have {_money(cash_and_bank_total)} across your personal cash and bank accounts as of today ({as_of}).",
            np=f"Tapai sanga aaja ({as_of}) samma personal cash ra bank account haru ma {_money(cash_and_bank_total)} cha.",
            ne=f"आज ({as_of}) सम्म तपाईंको व्यक्तिगत नगद र बैंक खाताहरूमा {_money(cash_and_bank_total)} छ।",
        ),
    ]

    if breakdown_lines:
        parts.append(
            _phrase(
                response_language_mode,
                en="Breakdown:",
                np="Breakdown:",
                ne="विवरण:",
            )
        )
        parts.extend(breakdown_lines[:6])

    if overdraft_lines:
        parts.append(
            _phrase(
                response_language_mode,
                en="Keep in mind: Some accounts are below zero or overdrafted, which affects your total available balance.",
                np="Keep in mind: Kehi account haru zero bhanda tala wa overdraft ma chan, jasle total available balance lai affect garcha.",
                ne="ध्यान दिनुहोस्: केही खाताहरू शून्यभन्दा तल वा ओभरड्राफ्टमा छन्, जसले तपाईंको कुल उपलब्ध ब्यालेन्सलाई असर गर्छ।",
            )
        )
        parts.extend(overdraft_lines[:4])

    parts.append(
        _phrase(
            response_language_mode,
            en=f"Summary: Total personal account balance is {_money(total_balance)}. This reflects app-recorded account balances.",
            np=f"Summary: Total personal account balance {_money(total_balance)} ho. Yo app ma record bhayeko account balance ho.",
            ne=f"सारांश: कुल व्यक्तिगत खाता ब्यालेन्स {_money(total_balance)} हो। यो एपमा रेकर्ड भएका खाता ब्यालेन्समा आधारित हो।",
        )
    )
    return "\n".join(parts)


def try_generate_deterministic_personal_response(
    conn: Connection,
    *,
    user_id: str,
    user_query: str,
) -> DeterministicPersonalChatResult:
    understanding = parse_personal_query_understanding(user_query)
    if understanding.intent != "balance_lookup":
        return DeterministicPersonalChatResult(
            handled=False,
            route="llm_fallback",
            intent=understanding.intent,
            resolution_status="fallback",
        )

    profile_id = _get_personal_profile_id(conn, user_id)
    if not profile_id:
        return DeterministicPersonalChatResult(
            handled=False,
            route="llm_fallback",
            intent=understanding.intent,
            resolution_status="fallback",
        )

    response_language_mode = detect_response_language_mode(user_query)
    reply = _personal_balance_reply(
        conn,
        user_id=user_id,
        profile_id=profile_id,
        response_language_mode=response_language_mode,
    )
    return DeterministicPersonalChatResult(
        handled=True,
        route="deterministic",
        intent=understanding.intent,
        reply=reply,
        resolution_status="resolved",
    )
