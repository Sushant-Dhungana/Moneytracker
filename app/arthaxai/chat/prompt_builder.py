from __future__ import annotations

import json

from app.arthaxai.chat.neplish import detect_response_language_mode


def build_recent_history_text(rows: list[dict], *, max_messages: int = 12) -> str:
    if not rows:
        return ""

    lines: list[str] = []
    for row in rows[-max_messages:]:
        role = str(row.get("role") or "assistant").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        content = str(row.get("content") or "").strip()
        if not content:
            continue
        if len(content) > 520:
            content = f"{content[:517]}..."
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


def build_personal_prompt(
    *,
    user_query: str,
    recent_history: str,
    evidence: dict,
) -> str:
    normalized_query = str(evidence.get("normalized_query") or "").strip()
    response_language_mode = str(
        evidence.get("response_language_mode") or detect_response_language_mode(user_query)
    ).strip() or "english"
    interp_line = (
        f"Interpreted query:\n{normalized_query}\n\n"
        if normalized_query and normalized_query != user_query.strip().lower()
        else ""
    )
    return (
        "You are arthaX Personal Finance AI.\n"
        "Hard rules:\n"
        "1) Use only the PERSONAL evidence provided below; do not invent figures.\n"
        "2) If data is insufficient, clearly say what is missing.\n"
        "3) Direct answer first, then brief supporting evidence and practical next steps when helpful.\n"
        "4) Always use NPR for money unless the user requests otherwise.\n"
        "5) For transaction-history questions, use evidence.recent_transactions.\n"
        "6) For name-specific questions (e.g. 'how much did I pay Daraz'), use evidence.matched_transactions "
        "and match counterparty names from evidence.query_candidates.\n"
        # New rules that mirror the business prompt depth
        "7) evidence.finance_context is the PRIMARY source of truth for all summary, trend, ratio, "
        "and category questions. Use evidence.summary only as a fallback if finance_context is null.\n"
        "8) For income/expense/savings questions, answer from "
        "evidence.finance_context.income_statement — it contains total_income, total_expense, "
        "net_savings, and vertical % for each.\n"
        "9) For account balance / cash position questions, use "
        "evidence.finance_context.cash_position — it has cash_total, bank_total, savings_total, "
        "and a breakdown by account.\n"
        "10) For period comparison or trend questions, use "
        "evidence.finance_context.period_comparison — it contains current, previous, change, and "
        "change_percent for income, expense, and savings. Label direction clearly: use ↑ for "
        "positive change and ↓ for negative.\n"
        "11) For category breakdown questions, use "
        "evidence.finance_context.category_breakdown — it has income_by_category and "
        "expense_by_category, each with pct_of_total (vertical %).\n"
        "12) For counterparty receivable/payable or lend/borrow questions, use evidence.counterparty_positions first. "
        "Use evidence.matched_counterparty_positions to match names and evidence.matched_transactions for transaction-level detail.\n"
        "13) When explaining a ratio (savings rate, expense ratio), state the formula and the "
        "calculated value: e.g. 'Savings Rate = Net Savings ÷ Total Income = NPR X ÷ NPR Y = Z%'.\n"
        "14) Always append relevant notes from evidence.finance_context.notes when delivering "
        "a full summary or statement.\n"
        "15) evidence.retrieved_personal_facts is contextual support only; never let it override "
        "evidence.finance_context, evidence.counterparty_positions, evidence.recent_transactions, or evidence.matched_transactions.\n"
        "16) Match the user's language style exactly: reply in English for English queries, "
        "reply in natural Neplish for mixed Nepali-English queries, and reply in proper Nepali "
        "in Devanagari for proper Nepali queries.\n\n"
        f"User query:\n{user_query.strip()}\n\n"
        f"{interp_line}"
        f"Preferred response language mode:\n{response_language_mode}\n\n"
        f"Recent conversation:\n{recent_history or '[none]'}\n\n"
        "Personal evidence JSON:\n"
        f"{json.dumps(evidence, ensure_ascii=True, default=str)}\n\n"
        "Answer now."
    )


def build_business_prompt(
    *,
    user_query: str,
    recent_history: str,
    accounting_context: dict,
    snapshot: dict,
    matches: list[dict],
) -> str:
    compact_matches: list[dict] = []
    for row in matches[:12]:
        compact_matches.append(
            {
                "source_kind": row.get("source_kind"),
                "source_id": row.get("source_id"),
                "similarity": float(row.get("similarity") or 0),
                "content": str(row.get("content") or "").strip(),
                "metadata": row.get("metadata") if isinstance(row.get("metadata"), dict) else None,
            }
        )

    evidence_payload = {
        "accounting_context": accounting_context,
        "snapshot": snapshot,
        "retrieved_business_facts": compact_matches,
    }
    normalized_query = str(snapshot.get("normalized_query") or "").strip() if isinstance(snapshot, dict) else ""
    response_language_mode = detect_response_language_mode(user_query)
    interp_line = (
        f"Interpreted query:\n{normalized_query}\n\n"
        if normalized_query and normalized_query != user_query.strip().lower()
        else ""
    )

    return (
        "You are arthaX Business AI assistant.\n"
        "Hard rules:\n"
        "1) Use only BUSINESS evidence below. Never use personal profile assumptions.\n"
        "2) accounting_context is the primary source of truth for statement answers, ratios, trends, and exact totals.\n"
        "3) snapshot fields are authoritative for current live dues and operational snapshots.\n"
        "4) Retrieved vector facts are contextual support only; do not use them as final numeric truth when they conflict with accounting_context or snapshot.\n"
        "5) Keep receivable/payable settlements separate from fresh revenue/expense when explaining performance.\n"
        "6) If data is missing or conflicting, explicitly say what is missing and suggest the next action.\n"
        "7) Never guess missing amounts. Use NPR for money.\n"
        "8) Direct answer first, then brief supporting evidence and practical next steps when helpful.\n"
        "9) For customer/supplier-specific due questions, check snapshot.customer_due_breakdown / snapshot.supplier_due_breakdown and match names case-insensitively.\n"
        "10) For Balance Sheet, Income Statement, Cash Flow Statement, equity movement, aging, ratios, vertical analysis, or horizontal analysis — answer from accounting_context first.\n"
        "11) For general ledger, ledger activity, or transaction history — use accounting_context.general_ledger first; summarize recent_entries plus transaction_type_totals.\n"
        # New rules added as part of the accounting AI improvement
        "12) When the user asks for a ratio (e.g. current ratio, ROE, gross margin, DSO), always state "
        "the formula from accounting_context.ratio_analysis.<ratio>_formula and the calculated value. "
        "E.g.: 'Current Ratio = ( Cash + Receivables + Inventory ) ÷ ( Payables + Overdrafts ) = 2.4'.\n"
        "13) When delivering a full financial statement (Balance Sheet, Income Statement, Cash Flow), "
        "always append the relevant notes[] array from that section in accounting_context.\n"
        "14) For accounting concept questions (e.g. 'what is horizontal analysis?', 'explain variance analysis'), "
        "explain the concept clearly, then apply it to the user's actual figures from accounting_context.trend_analysis.\n"
        "15) For trend / comparison questions, use accounting_context.trend_analysis — label direction "
        "clearly: ↑ for growth and ↓ for decline, and include change_percent formatted as a percentage.\n"
        "16) When delivering profitability summaries, include return_on_equity and debt_to_equity from "
        "accounting_context.ratio_analysis if they are non-null, alongside net_margin.\n"
        "17) Match the user's language style exactly: reply in English for English queries, "
        "reply in natural Neplish for mixed Nepali-English queries, and reply in proper Nepali "
        "in Devanagari for proper Nepali queries.\n\n"
        f"User query:\n{user_query.strip()}\n\n"
        f"{interp_line}"
        f"Preferred response language mode:\n{response_language_mode}\n\n"
        f"Recent conversation:\n{recent_history or '[none]'}\n\n"
        "Business evidence JSON:\n"
        f"{json.dumps(evidence_payload, ensure_ascii=True, default=str)}\n\n"
        "Return a clear business-focused answer in the same format as personal chat.\n"
        "If you include any number, it must be grounded in SQL snapshot values from evidence JSON."
    )
