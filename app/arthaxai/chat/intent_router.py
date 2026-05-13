from __future__ import annotations

import re
from dataclasses import dataclass

from app.arthaxai.chat.chat_scopes import ChatScope
from app.arthaxai.chat.neplish import normalize_neplish_text
from app.arthaxai.services.accounting_statement_service import classify_business_accounting_focus

_PERSONAL_FINANCE_QUERY_PATTERN = re.compile(
    r"\b(spend|spending|expense|expenses|income|budget|transaction|transactions|category|categories|"
    r"balance|cashflow|cash flow|saving|savings|report|analy[sz]e|insight|money|finance|financial|"
    r"monthly|weekly|week|yearly|year|salary|bill|bills|rent|grocery|food|transport|shopping|"
    r"medical|health|education|entertainment|compare|total|how much|this month|last month|"
    r"this year|last year)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ChatIntentRoute:
    scope: ChatScope
    label: str
    mode: str


def route_personal_chat_intent(query: str) -> ChatIntentRoute:
    normalized = normalize_neplish_text(query)
    mode = "finance" if _PERSONAL_FINANCE_QUERY_PATTERN.search(normalized) else "general"
    return ChatIntentRoute(scope="personal", label="personal_chat", mode=mode)


def route_business_chat_intent(query: str) -> ChatIntentRoute:
    normalized = normalize_neplish_text(query)
    focus = classify_business_accounting_focus(normalized)
    return ChatIntentRoute(scope="business", label="business_chat", mode=focus)
