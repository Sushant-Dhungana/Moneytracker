from __future__ import annotations

from difflib import SequenceMatcher
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Literal, Sequence

from psycopg import Connection

BusinessIntent = Literal[
    "monthly_report",
    "customer_due",
    "supplier_due",
    "party_transactions",
    "stock_quantity_lookup",
    "stock_existence",
    "product_price_lookup",
    "low_stock_check",
    "customer_purchase_total",
    "customer_invoice_due_lookup",
    "supplier_purchase_total",
    "fallback",
]
PartyKind = Literal["customer", "supplier"]
EntityType = Literal["product", "customer", "supplier"]
EntityTypeHint = Literal["product", "customer", "supplier", "party", "unknown"]
ResolutionStatus = Literal["resolved", "ambiguous", "not_found", "fallback"]

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_SHARED_NEPLISH_RULES_PATH = _PROJECT_ROOT / "shared" / "nlu" / "neplish_rules.json"
_SHARED_NEPLISH_RULES_FALLBACK = {
    "replacement_rules": [
        {"pattern": r"\b(yo)\s+(mahina|mahinaa|mahaina)\b", "replacement": "this month"},
        {"pattern": r"\b(yo)\s+(hapta)\b", "replacement": "this week"},
        {"pattern": r"\b(hjo)\b", "replacement": "hijo"},
        {"pattern": r"\b(hpta)\b", "replacement": "hapta"},
        {"pattern": r"\b(expnse)\b", "replacement": "expense"},
        {"pattern": r"\b(khrca)\b", "replacement": "kharcha"},
        {"pattern": r"\b(kharcha|karcha)\b", "replacement": "expense"},
        {"pattern": r"\b(aamdani|amdani)\b", "replacement": "income"},
        {"pattern": r"\b(talab)\b", "replacement": "salary"},
        {"pattern": r"\b(bachat)\b", "replacement": "savings"},
        {"pattern": r"\b(paisa|money)\b", "replacement": "money"},
        {"pattern": r"\b(mahina|mahinaa|mahaina)\b", "replacement": "month"},
        {"pattern": r"\b(hapta)\b", "replacement": "week"},
        {"pattern": r"\b(din|dina)\b", "replacement": "day"},
        {"pattern": r"\b(barsa|barsha|saal)\b", "replacement": "year"},
        {"pattern": r"\b(aaja|aja)\b", "replacement": "today"},
        {"pattern": r"\b(hijo)\b", "replacement": "yesterday"},
        {"pattern": r"\b(yo)\b", "replacement": "this"},
        {"pattern": r"\b(mero)\b", "replacement": "my"},
        {"pattern": r"\b(pichhlo|pichlo|pichllo|paila)\b", "replacement": "last"},
        {"pattern": r"\b(kati)\b", "replacement": "how much"},
        {"pattern": r"\b(kul|jamma)\b", "replacement": "total"},
    ],
    "finance_keywords": [
        "kharcha",
        "karcha",
        "aamdani",
        "amdani",
        "bachat",
        "paisa",
        "hisab",
        "artha",
        "mahina",
        "mahaina",
        "mahinaa",
        "barsa",
        "barsha",
        "saal",
        "aaja",
        "aja",
        "hijo",
        "hapta",
        "talab",
        "kati",
        "jamma",
        "kul",
        "tirnu",
        "tirna",
        "linu",
        "paunu",
        "baki",
        "sanga",
        "bata",
        "cha",
        "chaina",
        "xa",
        "chha",
    ],
    "cue_words": [
        "ko",
        "cha",
        "chha",
        "xa",
        "kati",
        "lai",
        "le",
        "sanga",
        "bata",
        "mahina",
        "mahaina",
        "mahinaa",
        "hapta",
        "aaja",
        "aja",
        "hijo",
        "tirnu",
        "tirna",
        "parney",
        "linu",
        "paunu",
        "baki",
        "hisab",
    ],
}


def _load_shared_neplish_rules() -> dict:
    try:
        with _SHARED_NEPLISH_RULES_PATH.open("r", encoding="utf-8") as fp:
            raw = json.load(fp)
        if isinstance(raw, dict):
            return raw
    except Exception:
        pass
    return _SHARED_NEPLISH_RULES_FALLBACK


def _build_shared_neplish_replacements(
    config: dict,
) -> tuple[tuple[re.Pattern[str], str], ...]:
    compiled: list[tuple[re.Pattern[str], str]] = []
    for item in config.get("replacement_rules") or []:
        if not isinstance(item, dict):
            continue
        pattern = str(item.get("pattern") or "").strip()
        replacement = str(item.get("replacement") or "")
        if not pattern:
            continue
        try:
            compiled.append((re.compile(pattern, re.IGNORECASE), replacement))
        except re.error:
            continue
    return tuple(compiled)


_SHARED_NEPLISH_RULES = _load_shared_neplish_rules()
NEPLISH_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = _build_shared_neplish_replacements(
    _SHARED_NEPLISH_RULES
)
_SHARED_NEPLISH_FINANCE_KEYWORDS = {
    str(item).strip().lower()
    for item in (_SHARED_NEPLISH_RULES.get("finance_keywords") or [])
    if str(item).strip()
}
_SHARED_NEPLISH_CUE_WORDS = {
    str(item).strip().lower()
    for item in (_SHARED_NEPLISH_RULES.get("cue_words") or [])
    if str(item).strip()
}

TRANSACTION_HINT_PATTERN = re.compile(
    r"\b(transaction|transactions|transaction history|history|ledger|sanga ko transaction)\b",
    re.IGNORECASE,
)
REPORT_HINT_PATTERN = re.compile(
    r"\b(report|summary|statement|overview|hisab|financial)\b",
    re.IGNORECASE,
)
REPORT_SCOPE_HINT_PATTERN = re.compile(
    r"\b(this month|last month|this week|last week|this year|last year|monthly)\b",
    re.IGNORECASE,
)
CUSTOMER_HINT_PATTERN = re.compile(r"\b(customer|customers|client|clients|receivable)\b", re.IGNORECASE)
SUPPLIER_HINT_PATTERN = re.compile(r"\b(supplier|suppliers|vendor|vendors|payable)\b", re.IGNORECASE)
CUSTOMER_DUE_PATTERN = re.compile(
    r"\b(linu parney|paunu parney|le tirnu parney|customer le tirnu)\b",
    re.IGNORECASE,
)
SUPPLIER_DUE_PATTERN = re.compile(
    r"\b(lai tirnu parney|lai tirna baki|supplier lai tirnu|supplier lai tirna|vendor lai tirnu|vendor lai tirna)\b",
    re.IGNORECASE,
)
GENERIC_DUE_PATTERN = re.compile(r"\b(baki|due|dues|outstanding|hisab)\b", re.IGNORECASE)
CUSTOMER_PURCHASE_PATTERN = re.compile(
    r"\b(customer|client)\b.*\b(purchase|invoice|buy|kin|kineko|garyo|gareko)\b"
    r"|\b(le)\b.*\b(purchase|invoice|buy|kineko|garyo|gareko)\b",
    re.IGNORECASE,
)
SUPPLIER_PURCHASE_PATTERN = re.compile(
    r"\b(supplier|vendor)\b.*\b(purchase|stock|inventory|ayo|aayo|kin|kineko|buy|saman|item|items|liyo|liye|liyeko|liya)\b"
    r"|\b(bata)\b.*\b(purchase|stock|inventory|ayo|aayo|kineko|buy|saman|item|items|liyo|liye|liyeko|liya)\b",
    re.IGNORECASE,
)
INVOICE_DUE_PATTERN = re.compile(
    r"\b(invoice)\b.*\b(due|baki|outstanding|tirnu|linu|payable|receivable)\b",
    re.IGNORECASE,
)
STOCK_HINT_PATTERN = re.compile(
    r"\b(stock|inventory|qty|quantity|available|availability|stock ma)\b",
    re.IGNORECASE,
)
PRICE_HINT_PATTERN = re.compile(r"\b(price|rate|selling price|cost)\b", re.IGNORECASE)
LOW_STOCK_HINT_PATTERN = re.compile(r"\b(low stock|low|minimum stock|kam stock)\b", re.IGNORECASE)
STOCK_EXISTENCE_HINT_PATTERN = re.compile(
    r"\b(cha ki chaina|cha ki|stock ma cha|available|in stock|out of stock|cha|chaina)\b",
    re.IGNORECASE,
)
SUPPLIER_HINT_PATTERN_STRONG = re.compile(
    r"\b(supplier|suppliers|vendor|vendors|payable|supplier lai|vendor lai|bata)\b",
    re.IGNORECASE,
)
CUSTOMER_HINT_PATTERN_STRONG = re.compile(
    r"\b(customer|customers|client|clients|receivable|customer le|le tirnu)\b",
    re.IGNORECASE,
)

NEP_ENGLISH_CUE_WORDS = _SHARED_NEPLISH_CUE_WORDS | _SHARED_NEPLISH_FINANCE_KEYWORDS | {
    "ko",
    "cha",
    "chha",
    "xa",
    "kati",
    "lai",
    "le",
    "sanga",
    "bata",
    "mahina",
    "hapta",
    "aaja",
    "hijo",
    "tirnu",
    "parney",
    "linu",
    "paunu",
}

ENTITY_PATTERN_TEMPLATES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?P<entity>[a-z0-9][a-z0-9\s\-&\.]{1,80}?)\s+ko\s+stock\b", re.IGNORECASE),
    re.compile(
        r"\b(?P<entity>[a-z0-9][a-z0-9\s\-&\.]{1,80}?)\s+stock\s+ma\b", re.IGNORECASE
    ),
    re.compile(r"\b(?P<entity>[a-z0-9][a-z0-9\s\-&\.]{1,80}?)\s+ko\s+price\b", re.IGNORECASE),
    re.compile(
        r"\b(?P<entity>[a-z0-9][a-z0-9\s\-&\.]{1,80}?)\s+ko\s+(due|baki|payable|receivable)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?P<entity>[a-z0-9][a-z0-9\s\-&\.]{1,80}?(?:\s+(supplier|vendor))?)\s+lai\s+(kati\s+)?(tirnu|tirna)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?P<entity>[a-z0-9][a-z0-9\s\-&\.]{1,80}?(?:\s+(customer|client))?)\s+le\s+(tirnu|kati\s+purchase|kati\s+invoice)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?P<entity>[a-z0-9][a-z0-9\s\-&\.]{1,80}?)\s+le\s+(yo\s+mahina\s+)?kati\s+(purchase|invoice)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?P<entity>[a-z0-9][a-z0-9\s\-&\.]{1,80}?)\s+bata\s+(yo\s+mahina\s+)?kati\s+(stock|purchase|inventory)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?P<entity>[a-z0-9][a-z0-9\s\-&\.]{1,80}?)\s+bata\s+(k\s*k|kk|ke\s*ke)?\s*(saman|item|items)\s+(liyo|liye|liyeko|liya)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?P<entity>[a-z0-9][a-z0-9\s\-&\.]{1,80}?)\s+sanga\s+ko\s+transaction\b",
        re.IGNORECASE,
    ),
)

ENTITY_TOKEN_STOPWORDS = {
    "this",
    "month",
    "week",
    "year",
    "today",
    "yesterday",
    "last",
    "previous",
    "how",
    "much",
    "report",
    "summary",
    "show",
    "kati",
    "pichhlo",
    "pichlo",
    "pichllo",
    "paila",
    "mero",
    "my",
    "ko",
    "le",
    "lai",
    "bata",
    "sanga",
    "stock",
    "inventory",
    "price",
    "rate",
    "due",
    "baki",
    "payable",
    "receivable",
    "customer",
    "supplier",
    "vendor",
    "client",
    "transaction",
    "transactions",
    "history",
    "ledger",
    "invoice",
    "purchase",
    "k",
    "kk",
    "keke",
    "ke",
    "saman",
    "item",
    "items",
    "liyo",
    "liye",
    "liyeko",
    "liya",
    "yo",
    "mahina",
    "hapta",
}

LOW_STOCK_THRESHOLD = 5

PARTY_NAME_STOPWORDS = {
    "this",
    "month",
    "week",
    "year",
    "today",
    "yesterday",
    "last",
    "previous",
    "how",
    "much",
    "report",
    "summary",
    "show",
    "dekhaunus",
    "dekhaunu",
    "dekha",
    "kati",
    "tirnu",
    "parney",
    "linu",
    "paunu",
    "customer",
    "client",
    "supplier",
    "vendor",
    "receivable",
    "payable",
    "transaction",
    "transactions",
    "history",
    "ledger",
    "k",
    "kk",
    "keke",
    "ke",
    "saman",
    "item",
    "items",
    "liyo",
    "liye",
    "liyeko",
    "liya",
    "sanga",
    "ko",
    "le",
    "lai",
    "xa",
    "cha",
    "malai",
    "mero",
    "please",
    "pichhlo",
    "pichlo",
    "pichllo",
    "paila",
    "mero",
    "my",
}


@dataclass(frozen=True)
class ParsedDateScope:
    label: str
    start: date | None
    end: date | None
    all_time: bool = False


@dataclass(frozen=True)
class BusinessQueryUnderstanding:
    raw_query: str
    normalized_query: str
    intent: BusinessIntent
    scope: ParsedDateScope | None
    entity_type_hint: EntityTypeHint
    entity_text_candidates: list[str]
    party_name_candidates: list[str]
    expects_customer: bool
    expects_supplier: bool
    neplish_style: bool


@dataclass(frozen=True)
class EntityMatch:
    id: str
    name: str
    kind: EntityType
    normalized_name: str
    score: float


@dataclass(frozen=True)
class EntityResolution:
    product: EntityMatch | None
    customer: EntityMatch | None
    supplier: EntityMatch | None
    selected: EntityMatch | None
    ambiguous_same_name: bool
    confidence: float | None
    status: ResolutionStatus
    clarification_prompt: str | None = None


@dataclass(frozen=True)
class DeterministicBusinessChatResult:
    handled: bool
    intent: BusinessIntent
    route: Literal["deterministic", "llm_fallback"]
    entity_type: EntityType | None = None
    entity_match_confidence: float | None = None
    resolution_status: ResolutionStatus | None = None
    reply: str | None = None
    warnings: list[str] | None = None


def _strip_accents(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def _normalize_for_match(value: str) -> str:
    normalized = _strip_accents(str(value or "").strip().lower())
    normalized = re.sub(r"[^a-z0-9\s]", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _sanitize_entity_candidate(value: str) -> str | None:
    normalized = _normalize_for_match(value)
    if not normalized:
        return None
    if len(normalized) < 2:
        return None
    if normalized in ENTITY_TOKEN_STOPWORDS:
        return None
    return normalized


def _extract_entity_text_candidates(raw_query: str) -> list[str]:
    normalized_raw = _normalize_for_match(raw_query or "")
    if not normalized_raw:
        return []

    candidates: list[str] = []
    for pattern in ENTITY_PATTERN_TEMPLATES:
        for match in pattern.finditer(normalized_raw):
            raw_entity = match.groupdict().get("entity") or ""
            candidate = _sanitize_entity_candidate(raw_entity)
            if candidate:
                candidates.append(candidate)

    tokens = [
        token
        for token in normalized_raw.split()
        if token and token not in ENTITY_TOKEN_STOPWORDS and not token.isdigit()
    ]
    max_n = min(4, len(tokens))
    for size in range(max_n, 0, -1):
        for idx in range(0, len(tokens) - size + 1):
            candidate = _sanitize_entity_candidate(" ".join(tokens[idx : idx + size]))
            if candidate:
                candidates.append(candidate)

    unique: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        unique.append(candidate)
    return unique


def _looks_neplish_style(query: str) -> bool:
    normalized = _normalize_for_match(query or "")
    if not normalized:
        return False
    tokens = set(normalized.split())
    return bool(tokens & NEP_ENGLISH_CUE_WORDS)


def normalize_neplish_business_query(
    query: str, *, protected_spans: Sequence[str] | None = None
) -> str:
    normalized = _normalize_for_match(query or "")
    token_map: dict[str, str] = {}

    if protected_spans:
        spans = sorted(
            {
                _normalize_for_match(span)
                for span in protected_spans
                if _sanitize_entity_candidate(span)
            },
            key=len,
            reverse=True,
        )
        for idx, span in enumerate(spans):
            placeholder = f"entitytoken{idx}"
            pattern = re.compile(rf"\b{re.escape(span)}\b")
            if pattern.search(normalized):
                normalized = pattern.sub(placeholder, normalized)
                token_map[placeholder] = span

    for pattern, replacement in NEPLISH_REPLACEMENTS:
        normalized = pattern.sub(replacement, normalized)

    for placeholder, span in token_map.items():
        normalized = normalized.replace(placeholder, span)

    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def _month_bounds(target: date) -> tuple[date, date]:
    start = date(target.year, target.month, 1)
    if target.month == 12:
        end = date(target.year + 1, 1, 1) - timedelta(days=1)
    else:
        end = date(target.year, target.month + 1, 1) - timedelta(days=1)
    return start, end


def _parse_date_scope(normalized_query: str, *, today: date) -> ParsedDateScope | None:
    if "this month" in normalized_query:
        start, end = _month_bounds(today)
        return ParsedDateScope(label="this month", start=start, end=end)

    if "last month" in normalized_query or "previous month" in normalized_query:
        if today.month == 1:
            target = date(today.year - 1, 12, 1)
        else:
            target = date(today.year, today.month - 1, 1)
        start, end = _month_bounds(target)
        return ParsedDateScope(label="last month", start=start, end=end)

    if "this year" in normalized_query:
        return ParsedDateScope(
            label="this year",
            start=date(today.year, 1, 1),
            end=date(today.year, 12, 31),
        )

    if "last year" in normalized_query or "previous year" in normalized_query:
        return ParsedDateScope(
            label="last year",
            start=date(today.year - 1, 1, 1),
            end=date(today.year - 1, 12, 31),
        )

    if "this week" in normalized_query:
        weekday = today.weekday()
        start = today - timedelta(days=weekday)
        end = start + timedelta(days=6)
        return ParsedDateScope(label="this week", start=start, end=end)

    if "last week" in normalized_query or "previous week" in normalized_query:
        weekday = today.weekday()
        this_week_start = today - timedelta(days=weekday)
        start = this_week_start - timedelta(days=7)
        end = start + timedelta(days=6)
        return ParsedDateScope(label="last week", start=start, end=end)

    if "today" in normalized_query:
        return ParsedDateScope(label="today", start=today, end=today)

    if "yesterday" in normalized_query:
        yday = today - timedelta(days=1)
        return ParsedDateScope(label="yesterday", start=yday, end=yday)

    match = re.search(r"\blast\s+(\d{1,3})\s+days\b", normalized_query)
    if match:
        days = max(1, min(365, int(match.group(1))))
        start = today - timedelta(days=days - 1)
        return ParsedDateScope(label=f"last {days} days", start=start, end=today)

    return None


def _infer_entity_type_hint(normalized_query: str) -> EntityTypeHint:
    if not normalized_query:
        return "unknown"

    product_score = 0
    customer_score = 0
    supplier_score = 0

    if STOCK_HINT_PATTERN.search(normalized_query) or PRICE_HINT_PATTERN.search(normalized_query):
        product_score += 3
    if LOW_STOCK_HINT_PATTERN.search(normalized_query):
        product_score += 2

    if CUSTOMER_HINT_PATTERN_STRONG.search(normalized_query):
        customer_score += 3
    if CUSTOMER_DUE_PATTERN.search(normalized_query):
        customer_score += 2
    if CUSTOMER_PURCHASE_PATTERN.search(normalized_query):
        customer_score += 2

    if SUPPLIER_HINT_PATTERN_STRONG.search(normalized_query):
        supplier_score += 3
    if SUPPLIER_DUE_PATTERN.search(normalized_query):
        supplier_score += 2
    if SUPPLIER_PURCHASE_PATTERN.search(normalized_query):
        supplier_score += 2

    best = max(product_score, customer_score, supplier_score)
    if best <= 0:
        if GENERIC_DUE_PATTERN.search(normalized_query) or TRANSACTION_HINT_PATTERN.search(normalized_query):
            return "party"
        return "unknown"

    tied = sum(1 for value in (product_score, customer_score, supplier_score) if value == best)
    if tied > 1:
        return "party"

    if best == product_score:
        return "product"
    if best == customer_score:
        return "customer"
    return "supplier"


def _detect_intent(normalized_query: str, *, entity_type_hint: EntityTypeHint) -> BusinessIntent:
    if not normalized_query:
        return "fallback"

    if TRANSACTION_HINT_PATTERN.search(normalized_query):
        return "party_transactions"

    if CUSTOMER_PURCHASE_PATTERN.search(normalized_query):
        return "customer_purchase_total"
    if SUPPLIER_PURCHASE_PATTERN.search(normalized_query):
        return "supplier_purchase_total"
    if INVOICE_DUE_PATTERN.search(normalized_query):
        return "customer_invoice_due_lookup"

    if STOCK_HINT_PATTERN.search(normalized_query) or entity_type_hint == "product":
        if PRICE_HINT_PATTERN.search(normalized_query):
            return "product_price_lookup"
        if LOW_STOCK_HINT_PATTERN.search(normalized_query):
            return "low_stock_check"
        if STOCK_EXISTENCE_HINT_PATTERN.search(normalized_query) and "how much" not in normalized_query:
            return "stock_existence"
        return "stock_quantity_lookup"

    customer_due_hint = bool(CUSTOMER_DUE_PATTERN.search(normalized_query)) or bool(
        CUSTOMER_HINT_PATTERN.search(normalized_query) and GENERIC_DUE_PATTERN.search(normalized_query)
    )
    supplier_due_hint = bool(SUPPLIER_DUE_PATTERN.search(normalized_query)) or bool(
        SUPPLIER_HINT_PATTERN.search(normalized_query) and GENERIC_DUE_PATTERN.search(normalized_query)
    )
    explicit_customer = bool(CUSTOMER_HINT_PATTERN_STRONG.search(normalized_query))
    explicit_supplier = bool(SUPPLIER_HINT_PATTERN_STRONG.search(normalized_query))

    if REPORT_HINT_PATTERN.search(normalized_query):
        if REPORT_SCOPE_HINT_PATTERN.search(normalized_query) or "month" in normalized_query:
            return "monthly_report"

    if explicit_customer and not explicit_supplier and "how much" in normalized_query:
        return "customer_due"
    if explicit_supplier and not explicit_customer and "how much" in normalized_query:
        return "supplier_due"

    if customer_due_hint and not supplier_due_hint:
        return "customer_due"
    if supplier_due_hint and not customer_due_hint:
        return "supplier_due"

    if customer_due_hint and supplier_due_hint:
        if "lai tirnu parney" in normalized_query or explicit_supplier:
            return "supplier_due"
        if "le tirnu parney" in normalized_query or explicit_customer:
            return "customer_due"
        return "customer_due"

    if REPORT_HINT_PATTERN.search(normalized_query):
        return "monthly_report"

    if GENERIC_DUE_PATTERN.search(normalized_query):
        if entity_type_hint == "supplier":
            return "supplier_due"
        return "customer_due"

    return "fallback"


def _extract_party_name_candidates(normalized_query: str) -> list[str]:
    return _extract_entity_text_candidates(normalized_query)


def parse_business_query_understanding(
    query: str,
    *,
    today: date | None = None,
) -> BusinessQueryUnderstanding:
    now = today or date.today()
    raw_query = query.strip()
    entity_text_candidates = _extract_entity_text_candidates(raw_query)
    normalized_query = normalize_neplish_business_query(
        raw_query,
        protected_spans=entity_text_candidates,
    )
    scope = _parse_date_scope(normalized_query, today=now)
    entity_type_hint = _infer_entity_type_hint(normalized_query)
    intent = _detect_intent(normalized_query, entity_type_hint=entity_type_hint)

    if intent == "monthly_report" and scope is None:
        start, end = _month_bounds(now)
        scope = ParsedDateScope(label="this month", start=start, end=end)
    elif intent == "party_transactions" and scope is None:
        scope = ParsedDateScope(label="all time", start=None, end=None, all_time=True)

    expects_customer = bool(CUSTOMER_HINT_PATTERN_STRONG.search(normalized_query)) or bool(
        CUSTOMER_DUE_PATTERN.search(normalized_query)
    ) or intent in {"customer_purchase_total", "customer_invoice_due_lookup"}
    expects_supplier = bool(SUPPLIER_HINT_PATTERN_STRONG.search(normalized_query)) or bool(
        SUPPLIER_DUE_PATTERN.search(normalized_query)
    ) or intent == "supplier_purchase_total"

    if (
        intent in {"customer_due", "supplier_due"}
        and GENERIC_DUE_PATTERN.search(normalized_query)
        and not expects_customer
        and not expects_supplier
    ):
        expects_customer = True
        expects_supplier = True

    return BusinessQueryUnderstanding(
        raw_query=raw_query,
        normalized_query=normalized_query,
        intent=intent,
        scope=scope,
        entity_type_hint=entity_type_hint,
        entity_text_candidates=entity_text_candidates,
        party_name_candidates=entity_text_candidates,
        expects_customer=expects_customer,
        expects_supplier=expects_supplier,
        neplish_style=_looks_neplish_style(raw_query),
    )


def _score_entity_match(candidate: str, entity_name: str, *, normalized_query: str) -> float:
    candidate_norm = _normalize_for_match(candidate)
    entity_norm = _normalize_for_match(entity_name)
    if not candidate_norm or not entity_norm:
        return 0.0

    padded_query = f" {normalized_query} "
    exact_score = 1.0 if candidate_norm == entity_norm else 0.0
    contains_score = 0.0
    if len(candidate_norm) >= 3 and candidate_norm in entity_norm:
        contains_score = max(contains_score, 0.9)
    if len(entity_norm) >= 3 and entity_norm in candidate_norm:
        contains_score = max(contains_score, 0.86)
    if entity_norm.startswith(candidate_norm) or candidate_norm.startswith(entity_norm):
        contains_score = max(contains_score, 0.88)

    ratio = SequenceMatcher(None, candidate_norm, entity_norm).ratio()
    query_ratio = SequenceMatcher(None, normalized_query, entity_norm).ratio()
    name_tokens = {token for token in entity_norm.split() if len(token) >= 2}
    candidate_tokens = {token for token in candidate_norm.split() if len(token) >= 2}
    overlap_ratio = (
        len(name_tokens & candidate_tokens) / max(1, len(name_tokens))
        if name_tokens
        else 0.0
    )

    blended = max(
        exact_score,
        contains_score,
        0.74 * ratio + 0.26 * overlap_ratio,
        0.62 * ratio + 0.24 * query_ratio + 0.14 * overlap_ratio,
    )
    if f" {entity_norm} " in padded_query:
        blended = max(blended, 0.98)
    return max(0.0, min(blended, 1.0))


def _rank_entity_matches(
    *,
    rows: Sequence[dict],
    kind: EntityType,
    understanding: BusinessQueryUnderstanding,
) -> list[EntityMatch]:
    candidates = understanding.entity_text_candidates or _extract_entity_text_candidates(
        understanding.normalized_query
    )
    ranked: list[EntityMatch] = []
    for row in rows:
        entity_id = str(row.get("id") or "").strip()
        entity_name = str(row.get("name") or "").strip()
        if not entity_id or not entity_name:
            continue

        entity_norm = _normalize_for_match(entity_name)
        if not entity_norm:
            continue

        possible_candidates = list(candidates)
        possible_candidates.append(understanding.normalized_query)
        best_score = 0.0
        for candidate in possible_candidates:
            score = _score_entity_match(
                candidate,
                entity_name,
                normalized_query=understanding.normalized_query,
            )
            best_score = max(best_score, score)

        if best_score <= 0:
            continue
        ranked.append(
            EntityMatch(
                id=entity_id,
                name=entity_name,
                kind=kind,
                normalized_name=entity_norm,
                score=best_score,
            )
        )

    ranked.sort(
        key=lambda item: (-item.score, -len(item.normalized_name), item.normalized_name, item.id)
    )
    return ranked


def _pick_best_match(
    ranked: Sequence[EntityMatch],
    *,
    threshold: float = 0.72,
    margin: float = 0.08,
) -> tuple[EntityMatch | None, bool, float | None]:
    if not ranked:
        return None, False, None
    top = ranked[0]
    if top.score < threshold:
        return None, False, top.score

    second = ranked[1] if len(ranked) > 1 else None
    if second and top.score - second.score < margin:
        return None, True, top.score

    return top, False, top.score


def _build_ambiguous_prompt(
    *,
    understanding: BusinessQueryUnderstanding,
    intent: BusinessIntent,
    customer_ranked: Sequence[EntityMatch],
    supplier_ranked: Sequence[EntityMatch],
    product_ranked: Sequence[EntityMatch],
) -> str:
    if intent in {
        "stock_quantity_lookup",
        "stock_existence",
        "product_price_lookup",
        "low_stock_check",
    }:
        names = ", ".join(item.name for item in product_ranked[:3])
        return _neplish_phrase(
            understanding,
            en=f"I found multiple close product matches: {names}. Please specify the exact product name.",
            np=f"Maile dherai milne product haru paye: {names}. Kripaya exact product name dinuhos.",
        )

    if intent in {"customer_due", "customer_purchase_total", "customer_invoice_due_lookup"}:
        names = ", ".join(item.name for item in customer_ranked[:3])
        return _neplish_phrase(
            understanding,
            en=f"I found multiple close customer matches: {names}. Please specify the exact customer name.",
            np=f"Maile dherai milne customer haru paye: {names}. Kripaya exact customer name dinuhos.",
        )

    if intent in {"supplier_due", "supplier_purchase_total"}:
        names = ", ".join(item.name for item in supplier_ranked[:3])
        return _neplish_phrase(
            understanding,
            en=f"I found multiple close supplier matches: {names}. Please specify the exact supplier name.",
            np=f"Maile dherai milne supplier haru paye: {names}. Kripaya exact supplier name dinuhos.",
        )

    names: list[str] = []
    if customer_ranked:
        names.append(f"customers: {', '.join(item.name for item in customer_ranked[:2])}")
    if supplier_ranked:
        names.append(f"suppliers: {', '.join(item.name for item in supplier_ranked[:2])}")
    joined = "; ".join(names) if names else "customers/suppliers"
    return _neplish_phrase(
        understanding,
        en=f"I found multiple close party matches ({joined}). Please clarify the exact name.",
        np=f"Maile customer/supplier list ma dherai milne naam paye ({joined}). Kripaya exact naam clear garnuhos.",
    )


def resolve_party_name_candidates(
    understanding: BusinessQueryUnderstanding,
    *,
    customers: Sequence[dict],
    suppliers: Sequence[dict],
    products: Sequence[dict] | None = None,
) -> EntityResolution:
    product_ranked = _rank_entity_matches(
        rows=products or [],
        kind="product",
        understanding=understanding,
    )
    customer_ranked = _rank_entity_matches(
        rows=customers,
        kind="customer",
        understanding=understanding,
    )
    supplier_ranked = _rank_entity_matches(
        rows=suppliers,
        kind="supplier",
        understanding=understanding,
    )

    product, product_ambiguous, product_conf = _pick_best_match(product_ranked)
    customer, customer_ambiguous, customer_conf = _pick_best_match(customer_ranked)
    supplier, supplier_ambiguous, supplier_conf = _pick_best_match(supplier_ranked)

    cross_ambiguous = False
    selected: EntityMatch | None = None
    confidence: float | None = None
    ambiguous_same_name = bool(
        customer and supplier and customer.normalized_name and customer.normalized_name == supplier.normalized_name
    )

    if understanding.intent in {
        "stock_quantity_lookup",
        "stock_existence",
        "product_price_lookup",
        "low_stock_check",
    }:
        if product_ambiguous:
            return EntityResolution(
                product=product,
                customer=customer,
                supplier=supplier,
                selected=None,
                ambiguous_same_name=False,
                confidence=product_conf,
                status="ambiguous",
                clarification_prompt=_build_ambiguous_prompt(
                    understanding=understanding,
                    intent=understanding.intent,
                    customer_ranked=customer_ranked,
                    supplier_ranked=supplier_ranked,
                    product_ranked=product_ranked,
                ),
            )
        if product:
            selected = product
            confidence = product.score
        return EntityResolution(
            product=product,
            customer=customer,
            supplier=supplier,
            selected=selected,
            ambiguous_same_name=False,
            confidence=confidence,
            status="resolved" if selected else "not_found",
        )

    if understanding.intent in {"supplier_due", "supplier_purchase_total"}:
        if supplier_ambiguous:
            return EntityResolution(
                product=product,
                customer=customer,
                supplier=supplier,
                selected=None,
                ambiguous_same_name=False,
                confidence=supplier_conf,
                status="ambiguous",
                clarification_prompt=_build_ambiguous_prompt(
                    understanding=understanding,
                    intent=understanding.intent,
                    customer_ranked=customer_ranked,
                    supplier_ranked=supplier_ranked,
                    product_ranked=product_ranked,
                ),
            )
        selected = supplier
        confidence = supplier.score if supplier else None

    elif understanding.intent in {"customer_purchase_total", "customer_invoice_due_lookup"}:
        if customer_ambiguous:
            return EntityResolution(
                product=product,
                customer=customer,
                supplier=supplier,
                selected=None,
                ambiguous_same_name=False,
                confidence=customer_conf,
                status="ambiguous",
                clarification_prompt=_build_ambiguous_prompt(
                    understanding=understanding,
                    intent=understanding.intent,
                    customer_ranked=customer_ranked,
                    supplier_ranked=supplier_ranked,
                    product_ranked=product_ranked,
                ),
            )
        selected = customer
        confidence = customer.score if customer else None

    elif understanding.intent in {"customer_due", "party_transactions"}:
        if customer_ambiguous or supplier_ambiguous:
            return EntityResolution(
                product=product,
                customer=customer,
                supplier=supplier,
                selected=None,
                ambiguous_same_name=ambiguous_same_name,
                confidence=max(customer_conf or 0.0, supplier_conf or 0.0) or None,
                status="ambiguous",
                clarification_prompt=_build_ambiguous_prompt(
                    understanding=understanding,
                    intent=understanding.intent,
                    customer_ranked=customer_ranked,
                    supplier_ranked=supplier_ranked,
                    product_ranked=product_ranked,
                ),
            )

        if customer and supplier:
            if ambiguous_same_name:
                return EntityResolution(
                    product=product,
                    customer=customer,
                    supplier=supplier,
                    selected=None,
                    ambiguous_same_name=True,
                    confidence=max(customer.score, supplier.score),
                    status="ambiguous",
                    clarification_prompt=_neplish_phrase(
                        understanding,
                        en=(
                            f"I found '{customer.name}' in both customer and supplier lists. "
                            "Please clarify whether you mean customer or supplier."
                        ),
                        np=(
                            f"'{customer.name}' customer ra supplier duitai list ma cha. "
                            "Kripaya customer ho ki supplier ho vanera clear garnuhos."
                        ),
                    ),
                )

            if understanding.expects_customer and not understanding.expects_supplier:
                selected = customer
            elif understanding.expects_supplier and not understanding.expects_customer:
                selected = supplier
            else:
                if abs(customer.score - supplier.score) < 0.08:
                    cross_ambiguous = True
                selected = customer if customer.score >= supplier.score else supplier
            confidence = selected.score if selected else None
        elif customer:
            selected = customer
            confidence = customer.score
        elif supplier:
            selected = supplier
            confidence = supplier.score

    if cross_ambiguous:
        return EntityResolution(
            product=product,
            customer=customer,
            supplier=supplier,
            selected=None,
            ambiguous_same_name=False,
            confidence=max(customer.score if customer else 0, supplier.score if supplier else 0) or None,
            status="ambiguous",
            clarification_prompt=_neplish_phrase(
                understanding,
                en=(
                    f"I found close matches in both customer and supplier lists: "
                    f"customer '{customer.name if customer else '-'}', supplier '{supplier.name if supplier else '-'}'. "
                    "Please clarify which one you meant."
                ),
                np=(
                    "Customer ra supplier duitai list ma milne naam bhetiyo: "
                    f"customer '{customer.name if customer else '-'}', supplier '{supplier.name if supplier else '-'}'. "
                    "Tapai le kun lai khojnu bhako ho clear garnuhos."
                ),
            ),
        )

    if selected:
        return EntityResolution(
            product=product,
            customer=customer,
            supplier=supplier,
            selected=selected,
            ambiguous_same_name=False,
            confidence=confidence,
            status="resolved",
        )

    return EntityResolution(
        product=product,
        customer=customer,
        supplier=supplier,
        selected=None,
        ambiguous_same_name=ambiguous_same_name,
        confidence=max(customer_conf or 0.0, supplier_conf or 0.0, product_conf or 0.0) or None,
        status="not_found",
    )


def _first_existing_relation(conn: Connection, candidates: list[str]) -> str | None:
    for relation in candidates:
        with conn.cursor() as cur:
            cur.execute("select to_regclass(%(relation)s) as rel", {"relation": relation})
            row = cur.fetchone() or {}
        if row.get("rel"):
            return relation
    return None


def _relation_has_column(conn: Connection, relation: str, column_name: str) -> bool:
    if "." not in relation:
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


def _fetch_active_parties(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    kind: PartyKind,
) -> list[dict]:
    relation = _first_existing_relation(
        conn,
        [f"business.{kind}s", f"public.{kind}s"],
    )
    if not relation:
        return []
    has_active_col = _relation_has_column(conn, relation, "is_active")
    active_filter = "and is_active = true" if has_active_col else ""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            select id::text as id, coalesce(nullif(trim(name), ''), %(fallback_name)s) as name
            from {relation}
            where user_id = %(user_id)s::uuid
              and profile_id = %(profile_id)s::uuid
              {active_filter}
            order by name asc
            """,
            {
                "user_id": user_id,
                "profile_id": profile_id,
                "fallback_name": kind.title(),
            },
        )
        return cur.fetchall() or []


def _fetch_active_products(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
) -> list[dict]:
    products_relation = _first_existing_relation(conn, ["business.products", "public.products"])
    if not products_relation:
        return []

    stock_state_relation = _first_existing_relation(
        conn, ["business.product_stock_state", "public.product_stock_state"]
    )
    has_active_col = _relation_has_column(conn, products_relation, "is_active")
    has_price_col = _relation_has_column(conn, products_relation, "price")
    has_selling_price_col = _relation_has_column(conn, products_relation, "selling_price")
    has_quantity_col = _relation_has_column(conn, products_relation, "quantity")
    has_stock_qty_col = bool(
        stock_state_relation and _relation_has_column(conn, stock_state_relation, "qty_on_hand")
    )
    has_avg_unit_cost_col = bool(
        stock_state_relation and _relation_has_column(conn, stock_state_relation, "avg_unit_cost")
    )

    active_filter = "and p.is_active = true" if has_active_col else ""
    join_stock_state = (
        f"left join {stock_state_relation} pss on pss.product_id = p.id"
        if stock_state_relation
        else ""
    )
    qty_expr = "0::numeric"
    if has_stock_qty_col and has_quantity_col:
        qty_expr = "coalesce(pss.qty_on_hand, p.quantity, 0)"
    elif has_stock_qty_col:
        qty_expr = "coalesce(pss.qty_on_hand, 0)"
    elif has_quantity_col:
        qty_expr = "coalesce(p.quantity, 0)"

    selling_price_expr = (
        "coalesce(p.selling_price, 0)" if has_selling_price_col else "null::numeric"
    )
    base_price_expr = "coalesce(p.price, 0)" if has_price_col else "null::numeric"
    avg_cost_expr = "coalesce(pss.avg_unit_cost, 0)" if has_avg_unit_cost_col else "null::numeric"

    with conn.cursor() as cur:
        cur.execute(
            f"""
            select
              p.id::text as id,
              coalesce(nullif(trim(p.name), ''), 'Product') as name,
              {qty_expr} as qty_on_hand,
              {selling_price_expr} as selling_price,
              {base_price_expr} as base_price,
              {avg_cost_expr} as avg_unit_cost
            from {products_relation} p
            {join_stock_state}
            where p.user_id = %(user_id)s::uuid
              and p.profile_id = %(profile_id)s::uuid
              {active_filter}
            order by p.name asc
            """,
            {"user_id": user_id, "profile_id": profile_id},
        )
        rows = cur.fetchall() or []
    return rows


def _to_money_or_na(value: float | None) -> str:
    if value is None:
        return "N/A"
    return _money(value)


def _neplish_phrase(understanding: BusinessQueryUnderstanding, *, en: str, np: str) -> str:
    return np if understanding.neplish_style else en


def _as_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _money(value: float) -> str:
    return f"NPR {value:,.2f}"


def _monthly_report_reply(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    scope: ParsedDateScope | None,
) -> str | None:
    postings_relation = _first_existing_relation(
        conn,
        ["business.ledger_postings", "public.ledger_postings"],
    )
    entries_relation = _first_existing_relation(
        conn,
        ["business.ledger_entries", "public.ledger_entries"],
    )
    if not postings_relation or not entries_relation:
        return None

    today = date.today()
    start = scope.start if scope and scope.start else date(today.year, today.month, 1)
    end = scope.end if scope and scope.end else today
    as_of = min(today, end)
    period_label = scope.label if scope else "this month"

    with conn.cursor() as cur:
        cur.execute(
            f"""
            select
              coalesce(sum(case
                when lp.leg_type in ('sales_revenue','income_bucket') and lp.direction='credit'
                then lp.amount else 0 end), 0) as income_total,
              coalesce(sum(case
                when lp.leg_type in ('cogs','expense_bucket') and lp.direction='debit'
                then lp.amount else 0 end), 0) as expense_total
            from {postings_relation} lp
            join {entries_relation} le on le.id = lp.entry_id
            where lp.user_id = %(user_id)s::uuid
              and lp.profile_id = %(profile_id)s::uuid
              and le.date between %(start_date)s and %(end_date)s
            """,
            {
                "user_id": user_id,
                "profile_id": profile_id,
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
            },
        )
        period_totals = cur.fetchone() or {}

        cur.execute(
            f"""
            select
              coalesce(
                sum(
                  case
                    when lp.leg_type='receivable' and lp.direction='debit' then lp.amount
                    when lp.leg_type='receivable' and lp.direction='credit' then -lp.amount
                    else 0
                  end
                ),
                0
              ) as receivable_due,
              coalesce(
                sum(
                  case
                    when lp.leg_type='payable' and lp.direction='credit' then lp.amount
                    when lp.leg_type='payable' and lp.direction='debit' then -lp.amount
                    else 0
                  end
                ),
                0
              ) as payable_due
            from {postings_relation} lp
            join {entries_relation} le on le.id = lp.entry_id
            where lp.user_id = %(user_id)s::uuid
              and lp.profile_id = %(profile_id)s::uuid
              and le.date <= %(as_of_date)s
            """,
            {
                "user_id": user_id,
                "profile_id": profile_id,
                "as_of_date": as_of.isoformat(),
            },
        )
        due_totals = cur.fetchone() or {}

    income_total = float(period_totals.get("income_total") or 0)
    expense_total = float(period_totals.get("expense_total") or 0)
    net_total = income_total - expense_total
    receivable_due = max(0.0, float(due_totals.get("receivable_due") or 0))
    payable_due = max(0.0, float(due_totals.get("payable_due") or 0))

    summary = "profit" if net_total >= 0 else "loss"
    return "\n".join(
        [
            f"Business report for {period_label} ({start.isoformat()} to {end.isoformat()}):",
            f"- Income: {_money(income_total)}",
            f"- Expense: {_money(expense_total)}",
            f"- Net: {_money(net_total)}",
            f"- Receivable due: {_money(receivable_due)}",
            f"- Payable due: {_money(payable_due)}",
            f"Summary: {summary.title()} {_money(abs(net_total))}. As of {as_of.isoformat()}.",
        ]
    )


def _party_due_reply(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    party: EntityMatch,
) -> str | None:
    postings_relation = _first_existing_relation(
        conn,
        ["business.ledger_postings", "public.ledger_postings"],
    )
    entries_relation = _first_existing_relation(
        conn,
        ["business.ledger_entries", "public.ledger_entries"],
    )
    if not postings_relation:
        return None

    if party.kind == "customer":
        due_expr = (
            "case when lp.direction='debit' then lp.amount "
            "when lp.direction='credit' then -lp.amount else 0 end"
        )
        leg_type = "receivable"
        title = "Customer due"
        due_label = "Pending receivable"
    else:
        due_expr = (
            "case when lp.direction='credit' then lp.amount "
            "when lp.direction='debit' then -lp.amount else 0 end"
        )
        leg_type = "payable"
        title = "Supplier due"
        due_label = "Pending payable"

    join_entries = ""
    last_tx_select = "null::text as last_tx_date"
    if entries_relation:
        join_entries = f"left join {entries_relation} le on le.id = lp.entry_id"
        last_tx_select = "max(le.date)::text as last_tx_date"

    with conn.cursor() as cur:
        cur.execute(
            f"""
            select
              coalesce(sum({due_expr}), 0) as due_amount,
              {last_tx_select}
            from {postings_relation} lp
            {join_entries}
            where lp.user_id = %(user_id)s::uuid
              and lp.profile_id = %(profile_id)s::uuid
              and lp.leg_type = %(leg_type)s
              and lp.ref_id = %(party_id)s::uuid
            """,
            {
                "user_id": user_id,
                "profile_id": profile_id,
                "leg_type": leg_type,
                "party_id": party.id,
            },
        )
        row = cur.fetchone() or {}

    due_amount = max(0.0, float(row.get("due_amount") or 0))
    last_tx_date = str(row.get("last_tx_date") or "").strip()
    as_of = date.today().isoformat()
    return "\n".join(
        [
            f"{title} for {party.name}:",
            f"- {due_label}: {_money(due_amount)}",
            f"- Last related transaction: {last_tx_date or 'Not available'}",
            f"Summary: {_money(due_amount)} outstanding. As of {as_of}.",
        ]
    )


def _party_transactions_reply(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    party: EntityMatch,
    scope: ParsedDateScope | None,
) -> str | None:
    postings_relation = _first_existing_relation(
        conn,
        ["business.ledger_postings", "public.ledger_postings"],
    )
    entries_relation = _first_existing_relation(
        conn,
        ["business.ledger_entries", "public.ledger_entries"],
    )
    if not postings_relation or not entries_relation:
        return None

    if party.kind == "customer":
        leg_type = "receivable"
        due_expr = (
            "case when lp.direction='debit' then lp.amount "
            "when lp.direction='credit' then -lp.amount else 0 end"
        )
    else:
        leg_type = "payable"
        due_expr = (
            "case when lp.direction='credit' then lp.amount "
            "when lp.direction='debit' then -lp.amount else 0 end"
        )

    with conn.cursor() as cur:
        cur.execute(
            f"""
            select
              coalesce(count(distinct lp.entry_id), 0)::int as tx_count,
              coalesce(
                sum(case when ({due_expr}) > 0 then ({due_expr}) else 0 end),
                0
              ) as due_added,
              coalesce(
                sum(case when ({due_expr}) < 0 then -({due_expr}) else 0 end),
                0
              ) as due_settled,
              coalesce(sum({due_expr}), 0) as net_due,
              max(le.date)::text as last_tx_date
            from {postings_relation} lp
            left join {entries_relation} le on le.id = lp.entry_id
            where lp.user_id = %(user_id)s::uuid
              and lp.profile_id = %(profile_id)s::uuid
              and lp.leg_type = %(leg_type)s
              and lp.ref_id = %(party_id)s::uuid
            """,
            {
                "user_id": user_id,
                "profile_id": profile_id,
                "leg_type": leg_type,
                "party_id": party.id,
            },
        )
        summary_row = cur.fetchone() or {}

        date_filter_sql = ""
        bind: dict[str, object] = {
            "user_id": user_id,
            "profile_id": profile_id,
            "leg_type": leg_type,
            "party_id": party.id,
            "limit": 20,
        }
        if scope and not scope.all_time and scope.start and scope.end:
            date_filter_sql = "and le.date between %(start_date)s and %(end_date)s"
            bind["start_date"] = scope.start.isoformat()
            bind["end_date"] = scope.end.isoformat()

        cur.execute(
            f"""
            select
              le.id::text as entry_id,
              le.date::text as tx_date,
              le.txn_type,
              coalesce(nullif(trim(le.description), ''), '') as description,
              coalesce(sum({due_expr}), 0) as due_change,
              max(le.created_at) as created_at
            from {postings_relation} lp
            join {entries_relation} le on le.id = lp.entry_id
            where lp.user_id = %(user_id)s::uuid
              and lp.profile_id = %(profile_id)s::uuid
              and lp.leg_type = %(leg_type)s
              and lp.ref_id = %(party_id)s::uuid
              {date_filter_sql}
            group by le.id, le.date, le.txn_type, le.description
            order by le.date desc, created_at desc
            limit %(limit)s
            """,
            bind,
        )
        tx_rows = cur.fetchall() or []

    tx_count = int(summary_row.get("tx_count") or 0)
    due_added = float(summary_row.get("due_added") or 0)
    due_settled = float(summary_row.get("due_settled") or 0)
    net_due = float(summary_row.get("net_due") or 0)
    last_tx_date = str(summary_row.get("last_tx_date") or "").strip()

    scope_label = scope.label if scope else "all time"
    lines: list[str] = [
        f"Transaction history for {party.kind} {party.name}:",
        f"- All-time related entries: {tx_count}",
        f"- All-time due added: {_money(due_added)}",
        f"- All-time due settled: {_money(due_settled)}",
        f"- Current outstanding: {_money(max(0.0, net_due))}",
        f"- Last related transaction: {last_tx_date or 'Not available'}",
    ]

    if tx_rows:
        lines.append(f"Recent transactions ({scope_label}, latest {len(tx_rows)}):")
        for row in tx_rows:
            tx_date = str(row.get("tx_date") or "").strip() or "-"
            txn_type = str(row.get("txn_type") or "entry").strip()
            description = str(row.get("description") or "").strip()
            due_change = float(row.get("due_change") or 0)
            if due_change > 0:
                flow_label = f"Due +{_money(due_change)}"
            elif due_change < 0:
                flow_label = f"Settled {_money(abs(due_change))}"
            else:
                flow_label = f"No due change ({_money(0)})"
            desc_suffix = f" | {description}" if description else ""
            lines.append(f"- {tx_date}: {txn_type} | {flow_label}{desc_suffix}")
    else:
        lines.append(f"- No {scope_label} transactions found for this party.")

    lines.append(f"Summary: As of {date.today().isoformat()}.")
    return "\n".join(lines)


def _product_stock_quantity_reply(
    understanding: BusinessQueryUnderstanding,
    *,
    product: dict,
) -> str:
    product_name = str(product.get("name") or "Product").strip() or "Product"
    qty = _as_float(product.get("qty_on_hand")) or 0.0
    status_line = _neplish_phrase(
        understanding,
        en=f"{product_name} stock quantity is {qty:.3f}.",
        np=f"{product_name} ko stock {qty:.3f} cha.",
    )
    return "\n".join(
        [
            status_line,
            f"Summary: As of {date.today().isoformat()}.",
        ]
    )


def _product_stock_existence_reply(
    understanding: BusinessQueryUnderstanding,
    *,
    product: dict,
) -> str:
    product_name = str(product.get("name") or "Product").strip() or "Product"
    qty = _as_float(product.get("qty_on_hand")) or 0.0
    if qty <= 0:
        line = _neplish_phrase(
            understanding,
            en=f"{product_name} is out of stock.",
            np=f"{product_name} stock chaina.",
        )
    else:
        line = _neplish_phrase(
            understanding,
            en=f"{product_name} is in stock ({qty:.3f}).",
            np=f"{product_name} stock ma cha ({qty:.3f}).",
        )
    return "\n".join([line, f"Summary: As of {date.today().isoformat()}."])


def _product_price_reply(
    understanding: BusinessQueryUnderstanding,
    *,
    product: dict,
) -> str:
    product_name = str(product.get("name") or "Product").strip() or "Product"
    selling_price = _as_float(product.get("selling_price"))
    base_price = _as_float(product.get("base_price"))
    avg_unit_cost = _as_float(product.get("avg_unit_cost"))
    qty = _as_float(product.get("qty_on_hand")) or 0.0

    lines = [
        _neplish_phrase(
            understanding,
            en=f"Price details for {product_name}:",
            np=f"{product_name} ko price details:",
        ),
        f"- Selling price: {_to_money_or_na(selling_price)}",
        f"- Base price: {_to_money_or_na(base_price)}",
        f"- Avg unit cost: {_to_money_or_na(avg_unit_cost)}",
        f"- Stock qty: {qty:.3f}",
        f"Summary: As of {date.today().isoformat()}.",
    ]
    return "\n".join(lines)


def _product_low_stock_reply(
    understanding: BusinessQueryUnderstanding,
    *,
    product: dict,
) -> str:
    product_name = str(product.get("name") or "Product").strip() or "Product"
    qty = _as_float(product.get("qty_on_hand")) or 0.0

    if qty <= 0:
        line = _neplish_phrase(
            understanding,
            en=f"{product_name} is out of stock.",
            np=f"{product_name} stock chaina.",
        )
    elif qty <= LOW_STOCK_THRESHOLD:
        line = _neplish_phrase(
            understanding,
            en=f"{product_name} has low stock.",
            np=f"{product_name} low stock ma cha.",
        )
    else:
        line = _neplish_phrase(
            understanding,
            en=f"{product_name} is not in low stock.",
            np=f"{product_name} low stock ma chaina.",
        )
    return "\n".join([line, f"- Current qty: {qty:.3f}", f"Summary: As of {date.today().isoformat()}."])


def _customer_purchase_total_reply(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    customer: EntityMatch,
    scope: ParsedDateScope | None,
) -> str | None:
    invoices_relation = _first_existing_relation(conn, ["business.invoices", "public.invoices"])
    if not invoices_relation:
        return None

    invoice_payments_relation = _first_existing_relation(
        conn, ["business.invoice_payments", "public.invoice_payments"]
    )
    has_customer_id = _relation_has_column(conn, invoices_relation, "customer_id")
    has_customer_name_snapshot = _relation_has_column(
        conn, invoices_relation, "customer_name_snapshot"
    )
    has_paid_amount = _relation_has_column(conn, invoices_relation, "paid_amount")
    has_due_amount = _relation_has_column(conn, invoices_relation, "due_amount")
    has_date_col = _relation_has_column(conn, invoices_relation, "date")

    if not has_customer_id and not has_customer_name_snapshot:
        return None

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

    filters: list[str] = []
    if has_customer_id:
        filters.append("i.customer_id = %(customer_id)s::uuid")
    if has_customer_name_snapshot:
        filters.append("lower(trim(coalesce(i.customer_name_snapshot, ''))) = %(customer_name_norm)s")
    customer_filter_sql = " or ".join(filters)

    date_filter_sql = ""
    bind: dict[str, object] = {
        "user_id": user_id,
        "profile_id": profile_id,
        "customer_id": customer.id,
        "customer_name_norm": customer.normalized_name,
    }
    if scope and not scope.all_time and scope.start and scope.end and has_date_col:
        date_filter_sql = "and i.date between %(start_date)s and %(end_date)s"
        bind["start_date"] = scope.start.isoformat()
        bind["end_date"] = scope.end.isoformat()

    with conn.cursor() as cur:
        cur.execute(
            f"""
            select
              coalesce(count(*), 0)::int as invoice_count,
              coalesce(sum(coalesce(i.total, 0)), 0) as total_purchase,
              coalesce(sum({paid_amount_expr}), 0) as paid_total,
              coalesce(sum({due_amount_expr}), 0) as due_total,
              max(i.date)::text as last_invoice_date
            from {invoices_relation} i
            {join_payments_sql}
            where i.user_id = %(user_id)s::uuid
              and i.profile_id = %(profile_id)s::uuid
              and ({customer_filter_sql})
              {date_filter_sql}
            """,
            bind,
        )
        row = cur.fetchone() or {}

    scope_label = scope.label if scope else "all time"
    invoice_count = int(row.get("invoice_count") or 0)
    total_purchase = float(row.get("total_purchase") or 0)
    paid_total = float(row.get("paid_total") or 0)
    due_total = max(0.0, float(row.get("due_total") or 0))
    last_invoice_date = str(row.get("last_invoice_date") or "").strip() or "Not available"

    return "\n".join(
        [
            f"Customer purchase summary for {customer.name} ({scope_label}):",
            f"- Invoice count: {invoice_count}",
            f"- Total purchase: {_money(total_purchase)}",
            f"- Total paid: {_money(paid_total)}",
            f"- Outstanding due: {_money(due_total)}",
            f"- Last invoice date: {last_invoice_date}",
            f"Summary: As of {date.today().isoformat()}.",
        ]
    )


def _customer_invoice_due_reply(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    customer: EntityMatch,
    scope: ParsedDateScope | None,
) -> str | None:
    invoices_relation = _first_existing_relation(conn, ["business.invoices", "public.invoices"])
    if not invoices_relation:
        return None

    invoice_payments_relation = _first_existing_relation(
        conn, ["business.invoice_payments", "public.invoice_payments"]
    )
    has_customer_id = _relation_has_column(conn, invoices_relation, "customer_id")
    has_customer_name_snapshot = _relation_has_column(
        conn, invoices_relation, "customer_name_snapshot"
    )
    has_paid_amount = _relation_has_column(conn, invoices_relation, "paid_amount")
    has_due_amount = _relation_has_column(conn, invoices_relation, "due_amount")
    has_date_col = _relation_has_column(conn, invoices_relation, "date")
    has_payment_status = _relation_has_column(conn, invoices_relation, "payment_status")
    if not has_customer_id and not has_customer_name_snapshot:
        return None

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

    filters: list[str] = []
    if has_customer_id:
        filters.append("i.customer_id = %(customer_id)s::uuid")
    if has_customer_name_snapshot:
        filters.append("lower(trim(coalesce(i.customer_name_snapshot, ''))) = %(customer_name_norm)s")
    customer_filter_sql = " or ".join(filters)

    date_filter_sql = ""
    status_filter_sql = ""
    bind: dict[str, object] = {
        "user_id": user_id,
        "profile_id": profile_id,
        "customer_id": customer.id,
        "customer_name_norm": customer.normalized_name,
    }
    if scope and not scope.all_time and scope.start and scope.end and has_date_col:
        date_filter_sql = "and i.date between %(start_date)s and %(end_date)s"
        bind["start_date"] = scope.start.isoformat()
        bind["end_date"] = scope.end.isoformat()
    if has_payment_status:
        status_filter_sql = "and i.payment_status in ('partial', 'due')"

    with conn.cursor() as cur:
        cur.execute(
            f"""
            select
              coalesce(count(*), 0)::int as outstanding_invoice_count,
              coalesce(sum({due_amount_expr}), 0) as outstanding_due,
              max(i.date)::text as last_invoice_date
            from {invoices_relation} i
            {join_payments_sql}
            where i.user_id = %(user_id)s::uuid
              and i.profile_id = %(profile_id)s::uuid
              and ({customer_filter_sql})
              {date_filter_sql}
              {status_filter_sql}
              and ({due_amount_expr}) > 0
            """,
            bind,
        )
        row = cur.fetchone() or {}

    outstanding_invoice_count = int(row.get("outstanding_invoice_count") or 0)
    outstanding_due = max(0.0, float(row.get("outstanding_due") or 0))
    last_invoice_date = str(row.get("last_invoice_date") or "").strip() or "Not available"
    scope_label = scope.label if scope else "all time"
    return "\n".join(
        [
            f"Outstanding invoice due for {customer.name} ({scope_label}):",
            f"- Outstanding invoices: {outstanding_invoice_count}",
            f"- Outstanding due: {_money(outstanding_due)}",
            f"- Last invoice date: {last_invoice_date}",
            f"Summary: As of {date.today().isoformat()}.",
        ]
    )


def _supplier_purchase_total_reply(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    supplier: EntityMatch,
    scope: ParsedDateScope | None,
) -> str | None:
    entries_relation = _first_existing_relation(conn, ["business.ledger_entries", "public.ledger_entries"])
    if not entries_relation:
        return None
    has_metadata = _relation_has_column(conn, entries_relation, "metadata")
    has_txn_type = _relation_has_column(conn, entries_relation, "txn_type")
    has_date_col = _relation_has_column(conn, entries_relation, "date")
    if not has_metadata or not has_txn_type:
        return None

    date_filter_sql = ""
    bind: dict[str, object] = {
        "user_id": user_id,
        "profile_id": profile_id,
        "supplier_id": supplier.id,
        "supplier_name_norm": supplier.normalized_name,
    }
    if scope and not scope.all_time and scope.start and scope.end and has_date_col:
        date_filter_sql = "and le.date between %(start_date)s and %(end_date)s"
        bind["start_date"] = scope.start.isoformat()
        bind["end_date"] = scope.end.isoformat()

    with conn.cursor() as cur:
        cur.execute(
            f"""
            select
              coalesce(count(*), 0)::int as purchase_entry_count,
              coalesce(sum(coalesce(le.amount, 0)), 0) as purchase_total,
              max(le.date)::text as last_purchase_date
            from {entries_relation} le
            where le.user_id = %(user_id)s::uuid
              and le.profile_id = %(profile_id)s::uuid
              and le.txn_type in ('inventory_in', 'stock_in', 'purchase')
              and (
                nullif(le.metadata->>'supplier_id', '') = %(supplier_id)s
                or lower(trim(coalesce(le.metadata->>'supplier_name', ''))) = %(supplier_name_norm)s
              )
              {date_filter_sql}
            """,
            bind,
        )
        row = cur.fetchone() or {}

    purchase_entry_count = int(row.get("purchase_entry_count") or 0)
    purchase_total = float(row.get("purchase_total") or 0)
    last_purchase_date = str(row.get("last_purchase_date") or "").strip() or "Not available"
    scope_label = scope.label if scope else "all time"
    return "\n".join(
        [
            f"Supplier purchase summary for {supplier.name} ({scope_label}):",
            f"- Purchase entries: {purchase_entry_count}",
            f"- Total purchase: {_money(purchase_total)}",
            f"- Last purchase date: {last_purchase_date}",
            f"Summary: As of {date.today().isoformat()}.",
        ]
    )


def _not_found_reply(understanding: BusinessQueryUnderstanding, expected: str) -> str:
    missing_name = (
        understanding.entity_text_candidates[0].strip()
        if understanding.entity_text_candidates
        else ""
    )
    if expected in {"customer name", "supplier name", "customer or supplier name"} and missing_name:
        return f"You don't have {missing_name} in your business."

    if understanding.neplish_style:
        return (
            f"Maile {expected} match garna sakenā. "
            "Kripaya exact name feri dinuhos."
        )
    return f"I could not match the {expected} in your query. Please provide the exact name and try again."


def try_generate_deterministic_business_response(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    user_query: str,
) -> DeterministicBusinessChatResult:
    understanding = parse_business_query_understanding(user_query)
    if understanding.intent == "fallback":
        return DeterministicBusinessChatResult(
            handled=False,
            route="llm_fallback",
            intent="fallback",
            resolution_status="fallback",
        )

    if understanding.intent == "monthly_report":
        reply = _monthly_report_reply(
            conn,
            user_id=user_id,
            profile_id=profile_id,
            scope=understanding.scope,
        )
        if not reply:
            return DeterministicBusinessChatResult(
                handled=False,
                route="llm_fallback",
                intent=understanding.intent,
                resolution_status="fallback",
            )
        return DeterministicBusinessChatResult(
            handled=True,
            route="deterministic",
            intent=understanding.intent,
            resolution_status="resolved",
            reply=reply,
        )

    products = _fetch_active_products(
        conn,
        user_id=user_id,
        profile_id=profile_id,
    )
    customers = _fetch_active_parties(
        conn,
        user_id=user_id,
        profile_id=profile_id,
        kind="customer",
    )
    suppliers = _fetch_active_parties(
        conn,
        user_id=user_id,
        profile_id=profile_id,
        kind="supplier",
    )
    resolution = resolve_party_name_candidates(
        understanding,
        customers=customers,
        suppliers=suppliers,
        products=products,
    )

    if resolution.status == "ambiguous" and resolution.clarification_prompt:
        return DeterministicBusinessChatResult(
            handled=True,
            route="deterministic",
            intent=understanding.intent,
            entity_type=resolution.product.kind if resolution.product else None,
            entity_match_confidence=resolution.confidence,
            resolution_status="ambiguous",
            reply=resolution.clarification_prompt,
        )

    selected_entity = resolution.selected
    if not selected_entity and understanding.intent in {
        "stock_quantity_lookup",
        "stock_existence",
        "product_price_lookup",
        "low_stock_check",
        "customer_due",
        "supplier_due",
        "party_transactions",
        "customer_purchase_total",
        "customer_invoice_due_lookup",
        "supplier_purchase_total",
    }:
        expected = "entity name"
        if understanding.intent in {
            "stock_quantity_lookup",
            "stock_existence",
            "product_price_lookup",
            "low_stock_check",
        }:
            expected = "product name"
        elif understanding.intent in {"customer_due", "customer_purchase_total", "customer_invoice_due_lookup"}:
            expected = "customer name"
        elif understanding.intent == "supplier_due" or understanding.intent == "supplier_purchase_total":
            expected = "supplier name"
        elif understanding.intent == "party_transactions":
            expected = "customer or supplier name"

        return DeterministicBusinessChatResult(
            handled=True,
            route="deterministic",
            intent=understanding.intent,
            entity_type=selected_entity.kind if selected_entity else None,
            entity_match_confidence=resolution.confidence,
            resolution_status="not_found",
            reply=_not_found_reply(understanding, expected),
        )

    if not selected_entity:
        return DeterministicBusinessChatResult(
            handled=False,
            route="llm_fallback",
            intent=understanding.intent,
            resolution_status="fallback",
        )

    if understanding.intent in {
        "stock_quantity_lookup",
        "stock_existence",
        "product_price_lookup",
        "low_stock_check",
    }:
        product_row = next(
            (row for row in products if str(row.get("id") or "").strip() == selected_entity.id),
            None,
        )
        if not product_row:
            return DeterministicBusinessChatResult(
                handled=True,
                route="deterministic",
                intent=understanding.intent,
                entity_type="product",
                entity_match_confidence=resolution.confidence,
                resolution_status="not_found",
                reply=_not_found_reply(understanding, "product name"),
            )

        if understanding.intent == "stock_quantity_lookup":
            stock_reply = _product_stock_quantity_reply(understanding, product=product_row)
        elif understanding.intent == "stock_existence":
            stock_reply = _product_stock_existence_reply(understanding, product=product_row)
        elif understanding.intent == "product_price_lookup":
            stock_reply = _product_price_reply(understanding, product=product_row)
        else:
            stock_reply = _product_low_stock_reply(understanding, product=product_row)

        return DeterministicBusinessChatResult(
            handled=True,
            route="deterministic",
            intent=understanding.intent,
            entity_type="product",
            entity_match_confidence=resolution.confidence,
            resolution_status="resolved",
            reply=stock_reply,
        )

    if understanding.intent in {"customer_due", "supplier_due"}:
        due_reply = _party_due_reply(
            conn,
            user_id=user_id,
            profile_id=profile_id,
            party=selected_entity,
        )
        if not due_reply:
            return DeterministicBusinessChatResult(
                handled=False,
                route="llm_fallback",
                intent=understanding.intent,
                entity_type=selected_entity.kind,
                entity_match_confidence=resolution.confidence,
                resolution_status="fallback",
            )
        return DeterministicBusinessChatResult(
            handled=True,
            route="deterministic",
            intent=understanding.intent,
            entity_type=selected_entity.kind,
            entity_match_confidence=resolution.confidence,
            resolution_status="resolved",
            reply=due_reply,
        )

    if understanding.intent == "customer_purchase_total" and selected_entity.kind == "customer":
        purchase_reply = _customer_purchase_total_reply(
            conn,
            user_id=user_id,
            profile_id=profile_id,
            customer=selected_entity,
            scope=understanding.scope,
        )
        if not purchase_reply:
            return DeterministicBusinessChatResult(
                handled=False,
                route="llm_fallback",
                intent=understanding.intent,
                entity_type=selected_entity.kind,
                entity_match_confidence=resolution.confidence,
                resolution_status="fallback",
            )
        return DeterministicBusinessChatResult(
            handled=True,
            route="deterministic",
            intent=understanding.intent,
            entity_type=selected_entity.kind,
            entity_match_confidence=resolution.confidence,
            resolution_status="resolved",
            reply=purchase_reply,
        )

    if understanding.intent == "customer_invoice_due_lookup" and selected_entity.kind == "customer":
        invoice_due_reply = _customer_invoice_due_reply(
            conn,
            user_id=user_id,
            profile_id=profile_id,
            customer=selected_entity,
            scope=understanding.scope,
        )
        if not invoice_due_reply:
            return DeterministicBusinessChatResult(
                handled=False,
                route="llm_fallback",
                intent=understanding.intent,
                entity_type=selected_entity.kind,
                entity_match_confidence=resolution.confidence,
                resolution_status="fallback",
            )
        return DeterministicBusinessChatResult(
            handled=True,
            route="deterministic",
            intent=understanding.intent,
            entity_type=selected_entity.kind,
            entity_match_confidence=resolution.confidence,
            resolution_status="resolved",
            reply=invoice_due_reply,
        )

    if understanding.intent == "supplier_purchase_total" and selected_entity.kind == "supplier":
        supplier_purchase_reply = _supplier_purchase_total_reply(
            conn,
            user_id=user_id,
            profile_id=profile_id,
            supplier=selected_entity,
            scope=understanding.scope,
        )
        if not supplier_purchase_reply:
            return DeterministicBusinessChatResult(
                handled=False,
                route="llm_fallback",
                intent=understanding.intent,
                entity_type=selected_entity.kind,
                entity_match_confidence=resolution.confidence,
                resolution_status="fallback",
            )
        return DeterministicBusinessChatResult(
            handled=True,
            route="deterministic",
            intent=understanding.intent,
            entity_type=selected_entity.kind,
            entity_match_confidence=resolution.confidence,
            resolution_status="resolved",
            reply=supplier_purchase_reply,
        )

    if understanding.intent == "party_transactions" and selected_entity.kind in {"customer", "supplier"}:
        tx_reply = _party_transactions_reply(
            conn,
            user_id=user_id,
            profile_id=profile_id,
            party=selected_entity,
            scope=understanding.scope,
        )
        if not tx_reply:
            return DeterministicBusinessChatResult(
                handled=False,
                route="llm_fallback",
                intent=understanding.intent,
                entity_type=selected_entity.kind,
                entity_match_confidence=resolution.confidence,
                resolution_status="fallback",
            )
        return DeterministicBusinessChatResult(
            handled=True,
            route="deterministic",
            intent=understanding.intent,
            entity_type=selected_entity.kind,
            entity_match_confidence=resolution.confidence,
            resolution_status="resolved",
            reply=tx_reply,
        )

    return DeterministicBusinessChatResult(
        handled=False,
        route="llm_fallback",
        intent=understanding.intent,
        entity_type=selected_entity.kind if selected_entity else None,
        entity_match_confidence=resolution.confidence,
        resolution_status="fallback",
    )
