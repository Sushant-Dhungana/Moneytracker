"""
personal_finance_context_service.py

Personal equivalent of accounting_statement_service.py.

Builds a rich, structured finance context for the personal AI pipeline,
mirroring the depth of the business accounting context so both chats
answer questions with the same intelligence — just scoped to their domain.

Output shape (returned by build_personal_finance_context):
  focus                   str   — detected query focus
  scope                   dict  — date range labels
  income_statement        dict  — income/expense/savings with vertical %
  cash_position           dict  — account balances by type
  savings_rate            float | None
  expense_ratio           float | None
  period_comparison       dict  — current vs previous period (horizontal analysis)
  category_breakdown      dict  — income and expense categories with vertical %
  top_counterparties      list  — top counterparties ranked by spend
  notes                   list  — auto-generated commentary
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Literal

from psycopg import Connection

from app.repositories.accounts_repository import get_account_balances
from app.services.summary_service import _first_existing_relation


PersonalFocus = Literal[
    "income_summary",
    "expense_summary",
    "savings_summary",
    "category_breakdown",
    "trend_analysis",
    "cash_position",
    "counterparty_query",
    "general",
]

# ── helpers ──────────────────────────────────────────────────────────────────

def _to_number(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _round_money(value: float) -> float:
    return round(float(value or 0), 2)


def _safe_divide(numerator: float, denominator: float) -> float | None:
    if abs(denominator) < 0.000001:
        return None
    return round(numerator / denominator, 4)


def _pct(value: float, total: float) -> float | None:
    return _safe_divide(value, total)


# ── focus classifier ─────────────────────────────────────────────────────────

import re

_FOCUS_PATTERNS: list[tuple[PersonalFocus, re.Pattern[str]]] = [
    ("income_summary",    re.compile(r"\b(income|earn|salary|received|aamdani|amdani|talab)\b", re.I)),
    ("expense_summary",   re.compile(r"\b(expense|spend|spent|kharcha|karcha|paid|payment)\b", re.I)),
    ("savings_summary",   re.compile(r"\b(saving|savings|bachat|save|net|remaining|left)\b", re.I)),
    ("category_breakdown",re.compile(r"\b(category|categories|breakdown|food|rent|transport|utility|shopping)\b", re.I)),
    ("trend_analysis",    re.compile(r"\b(compare|trend|vs|versus|horizontal|vertical|last month|previous)\b", re.I)),
    ("cash_position",     re.compile(r"\b(balance|cash|bank|account|paisa|money|how much do i have)\b", re.I)),
    ("counterparty_query",re.compile(r"\b(paid|to|from|daraz|esewa|khalti|fonepay|ncell|ntc|nabil|sanima)\b", re.I)),
]


def classify_personal_focus(query: str) -> PersonalFocus:
    normalized = str(query or "").strip()
    for focus, pattern in _FOCUS_PATTERNS:
        if pattern.search(normalized):
            return focus
    return "general"


# ── previous period window ───────────────────────────────────────────────────

def _build_previous_window(
    start: date | None, end: date | None
) -> tuple[date | None, date | None]:
    """
    Returns the equivalent prior period window.
    E.g. for 'this month' (May 1–31) → previous = Apr 1–30.
    """
    if not start or not end:
        return None, None
    span_days = max(1, (end - start).days + 1)
    previous_end = start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=span_days - 1)
    return previous_start, previous_end


# ── transaction fetcher ──────────────────────────────────────────────────────

def _fetch_personal_txn_totals(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str | None,
    start_date: date | None,
    end_date: date | None,
) -> dict[str, object]:
    """
    Fetches aggregated income/expense totals and category breakdowns
    for the given user and date window directly from transaction_feed_view.

    Returns:
        total_income        float
        total_expense       float
        income_by_category  list[dict]   — [{category, amount, txn_count}]
        expense_by_category list[dict]   — [{category, amount, txn_count}]
        top_counterparties  list[dict]   — [{name, amount, txn_count}]  (expense only)
        txn_count           int
    """
    feed_relation = _first_existing_relation(conn, ["public.transaction_feed_view"])
    if not feed_relation:
        return {
            "total_income": 0.0,
            "total_expense": 0.0,
            "income_by_category": [],
            "expense_by_category": [],
            "top_counterparties": [],
            "txn_count": 0,
        }

    bind: dict[str, object] = {"user_id": user_id}
    profile_filter = ""
    if profile_id:
        profile_filter = "and tfv.profile_id = %(profile_id)s::uuid"
        bind["profile_id"] = profile_id

    date_filter = ""
    if start_date:
        date_filter += " and tfv.date >= %(start_date)s"
        bind["start_date"] = start_date.isoformat()
    if end_date:
        date_filter += " and tfv.date <= %(end_date)s"
        bind["end_date"] = end_date.isoformat()

    with conn.cursor() as cur:
        # ── overall totals ────────────────────────────────────────────────
        cur.execute(
            f"""
            select
              coalesce(sum(case when tfv.txn_type = 'income'  then tfv.amount else 0 end), 0) as total_income,
              coalesce(sum(case when tfv.txn_type = 'expense' then tfv.amount else 0 end), 0) as total_expense,
              coalesce(count(*), 0) as txn_count
            from {feed_relation} tfv
            where tfv.user_id = %(user_id)s::uuid
              and tfv.txn_type in ('income', 'expense')
              {profile_filter}
              {date_filter}
            """,
            bind,
        )
        totals_row = cur.fetchone() or {}

        # ── income by category ────────────────────────────────────────────
        cur.execute(
            f"""
            select
              coalesce(nullif(trim(tfv.category_name), ''), 'Uncategorized') as category,
              coalesce(sum(tfv.amount), 0) as amount,
              count(*) as txn_count
            from {feed_relation} tfv
            where tfv.user_id = %(user_id)s::uuid
              and tfv.txn_type = 'income'
              {profile_filter}
              {date_filter}
            group by 1
            order by amount desc
            limit 10
            """,
            bind,
        )
        income_by_cat = cur.fetchall() or []

        # ── expense by category ───────────────────────────────────────────
        cur.execute(
            f"""
            select
              coalesce(nullif(trim(tfv.category_name), ''), 'Uncategorized') as category,
              coalesce(sum(tfv.amount), 0) as amount,
              count(*) as txn_count
            from {feed_relation} tfv
            where tfv.user_id = %(user_id)s::uuid
              and tfv.txn_type = 'expense'
              {profile_filter}
              {date_filter}
            group by 1
            order by amount desc
            limit 10
            """,
            bind,
        )
        expense_by_cat = cur.fetchall() or []

        # ── top counterparties (by expense amount) ────────────────────────
        cur.execute(
            f"""
            select
              coalesce(nullif(trim(tfv.counterparty_name), ''), 'Unknown') as name,
              coalesce(sum(tfv.amount), 0) as amount,
              count(*) as txn_count
            from {feed_relation} tfv
            where tfv.user_id = %(user_id)s::uuid
              and tfv.txn_type = 'expense'
              and tfv.counterparty_name is not null
              and trim(tfv.counterparty_name) != ''
              {profile_filter}
              {date_filter}
            group by 1
            order by amount desc
            limit 10
            """,
            bind,
        )
        top_counterparties = cur.fetchall() or []

    total_income = _round_money(_to_number(totals_row.get("total_income")))
    total_expense = _round_money(_to_number(totals_row.get("total_expense")))

    return {
        "total_income": total_income,
        "total_expense": total_expense,
        "txn_count": int(totals_row.get("txn_count") or 0),
        "income_by_category": [
            {
                "category": str(row.get("category") or "Uncategorized"),
                "amount": _round_money(_to_number(row.get("amount"))),
                "txn_count": int(row.get("txn_count") or 0),
                # vertical %: this category as % of total income
                "pct_of_total": _pct(_to_number(row.get("amount")), total_income),
            }
            for row in income_by_cat
        ],
        "expense_by_category": [
            {
                "category": str(row.get("category") or "Uncategorized"),
                "amount": _round_money(_to_number(row.get("amount"))),
                "txn_count": int(row.get("txn_count") or 0),
                # vertical %: this category as % of total expenses
                "pct_of_total": _pct(_to_number(row.get("amount")), total_expense),
            }
            for row in expense_by_cat
        ],
        "top_counterparties": [
            {
                "name": str(row.get("name") or "Unknown"),
                "amount": _round_money(_to_number(row.get("amount"))),
                "txn_count": int(row.get("txn_count") or 0),
                "pct_of_expenses": _pct(_to_number(row.get("amount")), total_expense),
            }
            for row in top_counterparties
        ],
    }


# ── account balance helper ────────────────────────────────────────────────────

def _fetch_personal_cash_position(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str | None,
) -> dict[str, object]:
    """
    Fetches personal account balances and groups by account type.
    Uses the existing get_account_balances repository function.
    """
    if not profile_id:
        return {
            "total_balance": 0.0,
            "cash_total": 0.0,
            "bank_total": 0.0,
            "savings_total": 0.0,
            "other_total": 0.0,
            "accounts": [],
        }

    rows = get_account_balances(conn, user_id, profile_id)

    cash_total = 0.0
    bank_total = 0.0
    savings_total = 0.0
    other_total = 0.0
    accounts = []

    for row in rows:
        name = str(row.get("account_name") or "Account").strip() or "Account"
        acct_type = str(row.get("account_type") or "").strip().lower()
        balance = _round_money(_to_number(row.get("current_balance")))

        if acct_type == "cash":
            cash_total += balance
        elif acct_type == "bank":
            bank_total += balance
        elif acct_type in {"savings", "fixed_deposit", "investment"}:
            savings_total += balance
        else:
            other_total += balance

        accounts.append({
            "name": name,
            "type": acct_type,
            "balance": balance,
        })

    total_balance = _round_money(cash_total + bank_total + savings_total + other_total)
    return {
        "total_balance": total_balance,
        "cash_total": _round_money(cash_total),
        "bank_total": _round_money(bank_total),
        "savings_total": _round_money(savings_total),
        "other_total": _round_money(other_total),
        "accounts": accounts,
    }


# ── main builder ──────────────────────────────────────────────────────────────

def build_personal_finance_context(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str | None,
    start_date: date | None,
    end_date: date | None,
    query: str = "",
) -> dict[str, object]:
    """
    Builds the full personal finance context.

    Equivalent of build_business_accounting_context but for personal data.
    Called from personal_tools.py to enrich the evidence passed to the LLM.

    Parameters:
        conn        — active DB connection
        user_id     — authenticated user UUID
        profile_id  — personal profile UUID (may be None if not found yet)
        start_date  — period start (None = all time)
        end_date    — period end   (None = all time)
        query       — raw user query string (used to classify focus)

    Returns a dict with keys: focus, scope, income_statement, cash_position,
    savings_rate, expense_ratio, period_comparison, category_breakdown,
    top_counterparties, notes.
    """
    focus = classify_personal_focus(query)
    previous_start, previous_end = _build_previous_window(start_date, end_date)

    # ── current period data ───────────────────────────────────────────────
    current = _fetch_personal_txn_totals(
        conn,
        user_id=user_id,
        profile_id=profile_id,
        start_date=start_date,
        end_date=end_date,
    )
    # ── previous period data (for horizontal / trend analysis) ────────────
    previous = _fetch_personal_txn_totals(
        conn,
        user_id=user_id,
        profile_id=profile_id,
        start_date=previous_start,
        end_date=previous_end,
    )
    # ── cash / account balances ───────────────────────────────────────────
    cash_position = _fetch_personal_cash_position(
        conn,
        user_id=user_id,
        profile_id=profile_id,
    )

    total_income = current["total_income"]
    total_expense = current["total_expense"]
    net_savings = _round_money(total_income - total_expense)

    # ── key personal ratios ───────────────────────────────────────────────
    # Savings Rate: what % of income is saved
    savings_rate = _safe_divide(net_savings, total_income)
    # Expense Ratio: what % of income goes to expenses
    expense_ratio = _safe_divide(total_expense, total_income)

    # ── period-over-period changes (horizontal analysis) ─────────────────
    prev_income = previous["total_income"]
    prev_expense = previous["total_expense"]
    prev_savings = _round_money(prev_income - prev_expense)

    income_change = _round_money(total_income - prev_income)
    expense_change = _round_money(total_expense - prev_expense)
    savings_change = _round_money(net_savings - prev_savings)

    # ── notes ─────────────────────────────────────────────────────────────
    notes: list[str] = [
        "Income and expense data comes from personal transaction records (transaction_feed_view).",
        "Savings = Total Income − Total Expenses for the selected period.",
        "Savings Rate = Net Savings ÷ Total Income.",
        "Expense Ratio = Total Expenses ÷ Total Income.",
        "Category vertical % = category amount ÷ total income or total expenses for the period.",
        "Period comparison uses the equivalent prior window (e.g. last month vs the month before that).",
    ]
    if not profile_id:
        notes.append("Personal profile not found — account balances are unavailable.")

    return {
        "focus": focus,
        "scope": {
            "start_date": start_date.isoformat() if start_date else None,
            "end_date": end_date.isoformat() if end_date else None,
            "comparison_start_date": previous_start.isoformat() if previous_start else None,
            "comparison_end_date": previous_end.isoformat() if previous_end else None,
            "as_of_date": date.today().isoformat(),
        },
        # ── personal income statement ─────────────────────────────────────
        # Mirrors business income_statement; income replaces revenue,
        # expenses replace operating expenses, net savings replaces net profit.
        "income_statement": {
            "total_income": total_income,
            "total_income_pct": 1.0 if total_income > 0 else None,   # base = 100%
            "total_expense": total_expense,
            "total_expense_pct": expense_ratio,                        # % of income
            "net_savings": net_savings,
            "net_savings_pct": savings_rate,                           # % of income
            "txn_count": current["txn_count"],
            "notes": [
                "Vertical % uses total income as the base (100%).",
                "Net Savings may be negative if expenses exceed income for the period.",
            ],
        },
        # ── cash / account position ───────────────────────────────────────
        # Mirrors business balance_sheet cash_and_bank section.
        "cash_position": cash_position,
        # ── key ratios ────────────────────────────────────────────────────
        "ratios": {
            "savings_rate": savings_rate,
            "savings_rate_formula": "Net Savings ÷ Total Income",
            "expense_ratio": expense_ratio,
            "expense_ratio_formula": "Total Expenses ÷ Total Income",
        },
        # ── horizontal analysis: current vs previous period ───────────────
        "period_comparison": {
            "income": {
                "current": total_income,
                "previous": prev_income,
                "change": income_change,
                "change_percent": _safe_divide(income_change, prev_income),
            },
            "expense": {
                "current": total_expense,
                "previous": prev_expense,
                "change": expense_change,
                "change_percent": _safe_divide(expense_change, prev_expense),
            },
            "savings": {
                "current": net_savings,
                "previous": prev_savings,
                "change": savings_change,
                "change_percent": _safe_divide(savings_change, prev_savings),
            },
            "notes": [
                "Horizontal analysis: change_percent shows period-over-period growth rate.",
                "Positive savings change_percent means you saved more than the prior period.",
            ],
        },
        # ── category breakdown (vertical analysis) ────────────────────────
        "category_breakdown": {
            "income_by_category": current["income_by_category"],
            "expense_by_category": current["expense_by_category"],
        },
        # ── top counterparties ────────────────────────────────────────────
        "top_counterparties": current["top_counterparties"],
        "notes": notes,
    }
