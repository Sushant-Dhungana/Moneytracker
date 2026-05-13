from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import re

from psycopg import Connection

from app.arthaxai.chat.intent_router import route_business_chat_intent
from app.arthaxai.chat.neplish import detect_response_language_mode
from app.arthaxai.chat.response_formatter import format_business_reply_like_personal
from app.core.config import Settings
from app.core.errors import ApiError
from app.arthaxai.services.accounting_statement_service import build_business_accounting_context
from app.arthaxai.services.ai_business_query_service import (
    parse_business_query_understanding,
    try_generate_deterministic_business_response,
)
from app.arthaxai.services.ai_business_vector_service import (
    _first_existing_relation,
    collect_business_live_snapshot,
)
from app.arthaxai.tools.business_vector_tools import get_business_vector_context

_INVENTORY_ONLY_INTENTS = {
    "stock_quantity_lookup",
    "stock_existence",
    "product_price_lookup",
    "low_stock_check",
}

_MANAGE_CATEGORY_QUERY_PATTERN = re.compile(
    r"\b(show|list|what|which|available|manage|heading|headings|group|groups|all)\b.*\b(category|categories)\b"
    r"|\b(category|categories)\b.*\b(show|list|what|which|available|manage|heading|headings|group|groups|all)\b",
    re.IGNORECASE,
)


def _money(value: object) -> str:
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        amount = 0.0
    return f"NPR {amount:,.2f}"


def _business_phrase(mode: str, *, en: str, np: str, ne: str | None = None) -> str:
    if mode == "nepali":
        return ne or np
    if mode == "neplish":
        return np
    return en


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


def _unified_business_categories_relation(conn: Connection) -> str | None:
    return _first_existing_relation(conn, ["public.business_categories", "business.business_categories"])


def _legacy_category_relation_for_domain(conn: Connection, domain: str) -> str | None:
    normalized = str(domain or "").strip().lower()
    if normalized == "product":
        return _first_existing_relation(conn, ["business.product_categories", "public.product_categories"])
    if normalized == "customer":
        return _first_existing_relation(conn, ["business.customer_categories", "public.customer_categories"])
    if normalized == "supplier":
        return _first_existing_relation(conn, ["business.supplier_categories", "public.supplier_categories"])
    if normalized in {"income", "expense"}:
        return _first_existing_relation(conn, ["personal.categories", "public.categories"])
    return None


def _detect_manage_category_domains(query: str) -> list[str]:
    normalized = str(query or "").strip().lower()
    domains: list[str] = []
    if any(token in normalized for token in ["product", "products", "stock", "inventory"]):
        domains.append("product")
    if any(token in normalized for token in ["customer", "customers", "client", "clients"]):
        domains.append("customer")
    if any(token in normalized for token in ["supplier", "suppliers", "vendor", "vendors"]):
        domains.append("supplier")
    if any(token in normalized for token in ["income", "sales", "revenue"]):
        domains.append("income")
    if any(token in normalized for token in ["expense", "expenses", "purchase", "purchases", "cost"]):
        domains.append("expense")
    return domains or ["product", "customer", "supplier", "income", "expense"]


def _fetch_business_manage_categories(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    domains: list[str],
) -> list[dict]:
    items: list[dict] = []
    unified_relation = _unified_business_categories_relation(conn)
    if unified_relation:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                select
                  id::text as id,
                  domain::text as domain,
                  name,
                  parent_id::text as parent_id
                from {unified_relation}
                where user_id = %(user_id)s::uuid
                  and profile_id = %(profile_id)s::uuid
                  and domain = any(%(domains)s::text[])
                  and is_active = true
                order by domain asc, created_at asc, name asc
                """,
                {"user_id": user_id, "profile_id": profile_id, "domains": domains},
            )
            return cur.fetchall() or []

    for domain in domains:
        relation = _legacy_category_relation_for_domain(conn, domain)
        if not relation:
            continue
        has_profile_id = _relation_has_column(conn, relation, "profile_id")
        has_parent_id = _relation_has_column(conn, relation, "parent_id")
        has_type = _relation_has_column(conn, relation, "type")
        has_is_active = _relation_has_column(conn, relation, "is_active")
        where_parts = ["user_id = %(user_id)s::uuid"]
        if has_profile_id:
            where_parts.append("profile_id = %(profile_id)s::uuid")
        if has_type and domain in {"income", "expense"}:
            where_parts.append("type = %(domain)s::text")
        if has_is_active:
            where_parts.append("is_active = true")
        with conn.cursor() as cur:
            cur.execute(
                f"""
                select
                  id::text as id,
                  %(domain)s::text as domain,
                  name,
                  {"parent_id::text" if has_parent_id else "null::text"} as parent_id
                from {relation}
                where {" and ".join(where_parts)}
                order by created_at asc, name asc
                """,
                {
                    "user_id": user_id,
                    "profile_id": profile_id,
                    "domain": domain,
                },
            )
            items.extend(cur.fetchall() or [])
    return items


def _is_manage_category_query(query: str) -> bool:
    normalized = str(query or "").strip().lower()
    if "category" not in normalized and "categories" not in normalized:
        return False
    return bool(_MANAGE_CATEGORY_QUERY_PATTERN.search(normalized)) or normalized in {
        "categories",
        "category",
        "expense categories",
        "income categories",
        "product categories",
        "customer categories",
        "supplier categories",
    }


def _build_business_manage_category_reply(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    query: str,
    response_language_mode: str,
) -> str | None:
    if not _is_manage_category_query(query):
        return None

    domains = _detect_manage_category_domains(query)
    rows = _fetch_business_manage_categories(
        conn,
        user_id=user_id,
        profile_id=profile_id,
        domains=domains,
    )
    if not rows:
        return "{}\n{}".format(
            _business_phrase(
                response_language_mode,
                en="Manage Category has no active categories for this business profile right now.",
                np="Yo business profile ko Manage Category ma ahile active categories chainan.",
                ne="यो व्यवसाय प्रोफाइलको Manage Category मा अहिले active categories छैनन्।",
            ),
            _business_phrase(
                response_language_mode,
                en=f"Summary: As of {date.today().isoformat()}.",
                np=f"Summary: {date.today().isoformat()} samma.",
                ne=f"सारांश: {date.today().isoformat()} सम्म।",
            ),
        )

    grouped: dict[str, list[dict]] = {}
    for row in rows:
        domain = str(row.get("domain") or "").strip().lower() or "other"
        grouped.setdefault(domain, []).append(row)

    lines = [
        _business_phrase(
            response_language_mode,
            en="Manage Category headings and categories for your business:",
            np="Tapai ko business ko Manage Category headings ra categories:",
            ne="तपाईंको व्यवसायका Manage Category headings र categories:",
        )
    ]
    total_categories = 0
    for domain in domains:
        domain_rows = grouped.get(domain, [])
        if not domain_rows:
            continue
        total_categories += len(domain_rows)
        by_parent: dict[str | None, list[dict]] = {}
        for row in domain_rows:
            parent_id = str(row.get("parent_id") or "").strip() or None
            by_parent.setdefault(parent_id, []).append(row)

        top_level = by_parent.get(None, [])
        lines.append(
            _business_phrase(
                response_language_mode,
                en=f"- {domain.title()} categories:",
                np=f"- {domain.title()} categories:",
                ne=f"- {domain.title()} categories:",
            )
        )
        if top_level:
            for parent in top_level:
                parent_name = str(parent.get("name") or "Category").strip() or "Category"
                children = by_parent.get(str(parent.get("id") or "").strip() or None, [])
                if children:
                    child_names = ", ".join(
                        str(child.get("name") or "Category").strip() or "Category"
                        for child in children[:8]
                    )
                    extra = len(children) - 8
                    suffix = (
                        _business_phrase(
                            response_language_mode,
                            en=f", plus {extra} more",
                            np=f", plus {extra} more",
                            ne=f", थप {extra} वटा",
                        )
                        if extra > 0
                        else ""
                    )
                    lines.append(f"- {parent_name} -> {child_names}{suffix}")
                else:
                    lines.append(f"- {parent_name}")
        else:
            flat_names = ", ".join(
                str(row.get("name") or "Category").strip() or "Category"
                for row in domain_rows[:10]
            )
            extra = len(domain_rows) - 10
            suffix = (
                _business_phrase(
                    response_language_mode,
                    en=f", plus {extra} more",
                    np=f", plus {extra} more",
                    ne=f", थप {extra} वटा",
                )
                if extra > 0
                else ""
            )
            lines.append(f"- {flat_names}{suffix}")

    lines.append(
        _business_phrase(
            response_language_mode,
            en=f"Summary: {total_categories} active categories from Manage Category. As of {date.today().isoformat()}.",
            np=f"Summary: Manage Category ma {total_categories} active categories chan. {date.today().isoformat()} samma.",
            ne=f"सारांश: Manage Category मा {total_categories} active categories छन्। {date.today().isoformat()} सम्म।",
        )
    )
    return "\n".join(lines)


def _build_generic_business_history_reply(
    accounting_context: dict | None,
    *,
    scope_label: str | None,
    response_language_mode: str,
) -> str | None:
    if not isinstance(accounting_context, dict):
        return None
    general_ledger = accounting_context.get("general_ledger")
    if not isinstance(general_ledger, dict) or not general_ledger.get("available"):
        return None

    scope_summary = general_ledger.get("scope_summary") if isinstance(general_ledger.get("scope_summary"), dict) else {}
    recent_entries = list(general_ledger.get("recent_entries") or [])
    txn_type_totals = list(general_ledger.get("transaction_type_totals") or [])
    entry_count = int(scope_summary.get("entries_count") or 0)
    gross_amount = _money(scope_summary.get("gross_amount"))
    first_entry_date = str(scope_summary.get("first_entry_date") or "").strip() or "Not available"
    last_entry_date = str(scope_summary.get("last_entry_date") or "").strip() or "Not available"
    resolved_scope_label = str(scope_label or "selected period").strip() or "selected period"

    # Empty-state: if no entries exist for the period, return a clear message
    # instead of showing "0 entries / NPR 0.00 / Not available to Not available"
    # which is confusing and looks like a broken response.
    if entry_count == 0:
        scope_summary = general_ledger.get("scope_summary") if isinstance(general_ledger.get("scope_summary"), dict) else {}
        checked_start = str(scope_summary.get("first_entry_date") or "").strip()
        lines = [
            _business_phrase(
                response_language_mode,
                en=f"No transactions were recorded for {resolved_scope_label}.",
                np=f"{resolved_scope_label} ko lagi kunai transaction record bhayena.",
                ne=f"{resolved_scope_label} का लागि कुनै transaction record भएन।",
            ),
            _business_phrase(
                response_language_mode,
                en=f"- Period checked: {resolved_scope_label}",
                np=f"- Check gareko period: {resolved_scope_label}",
                ne=f"- जाँच गरिएको अवधि: {resolved_scope_label}",
            ),
            _business_phrase(
                response_language_mode,
                en="- If your entries are from a different period, try asking about 'this month', 'this year', or a specific date range.",
                np="- Entry haru arko period ko ho bhane 'this month', 'this year', wa specific date range sodhnuhos.",
                ne="- Entry हरू अर्को अवधिका हुन् भने 'this month', 'this year', वा specific date range सोध्नुहोस्।",
            ),
            _business_phrase(
                response_language_mode,
                en=f"Summary: As of {date.today().isoformat()}.",
                np=f"Summary: {date.today().isoformat()} samma.",
                ne=f"सारांश: {date.today().isoformat()} सम्म।",
            ),
        ]
        return "\n".join(lines)

    lines = [
        _business_phrase(
            response_language_mode,
            en=f"Transaction history for {resolved_scope_label}:",
            np=f"Transaction history for {resolved_scope_label}:",
            ne=f"{resolved_scope_label} को transaction history:",
        ),
        _business_phrase(
            response_language_mode,
            en=f"- Entries recorded: {entry_count}",
            np=f"- Entries recorded: {entry_count}",
            ne=f"- Record भएका entries: {entry_count}",
        ),
        _business_phrase(
            response_language_mode,
            en=f"- Total amount: {gross_amount}",
            np=f"- Total amount: {gross_amount}",
            ne=f"- कुल रकम: {gross_amount}",
        ),
        _business_phrase(
            response_language_mode,
            en=f"- Period covered by matching entries: {first_entry_date} to {last_entry_date}",
            np=f"- Matching entries ko period: {first_entry_date} to {last_entry_date}",
            ne=f"- Matching entries को अवधि: {first_entry_date} देखि {last_entry_date} सम्म",
        ),
    ]

    if txn_type_totals:
        lines.append(
            _business_phrase(
                response_language_mode,
                en="Breakdown by type:",
                np="Type anusar breakdown:",
                ne="प्रकार अनुसार breakdown:",
            )
        )
        for row in txn_type_totals[:8]:
            txn_type = str(row.get("txn_type") or "entry").replace("_", " ").title()
            lines.append(
                _business_phrase(
                    response_language_mode,
                    en=f"- {txn_type}: {int(row.get('entry_count') or 0)} entries, {_money(row.get('total_amount'))}",
                    np=f"- {txn_type}: {int(row.get('entry_count') or 0)} entries, {_money(row.get('total_amount'))}",
                    ne=f"- {txn_type}: {int(row.get('entry_count') or 0)} entries, {_money(row.get('total_amount'))}",
                )
            )

    if recent_entries:
        lines.append(
            _business_phrase(
                response_language_mode,
                en=f"Recent entries ({resolved_scope_label}, latest {min(len(recent_entries), 8)}):",
                np=f"Recent entries ({resolved_scope_label}, latest {min(len(recent_entries), 8)}):",
                ne=f"हालका entries ({resolved_scope_label}, पछिल्ला {min(len(recent_entries), 8)}):",
            )
        )
        for row in recent_entries[:8]:
            entry_date = str(row.get("date") or "").strip() or "-"
            txn_type = str(row.get("txn_type") or "entry").replace("_", " ").title()
            amount = _money(row.get("amount"))
            description = str(row.get("description") or "").strip()
            detail = f"- {entry_date} | {txn_type} | {amount}"
            if description:
                detail += f" | {description}"
            lines.append(detail)

    lines.append(
        _business_phrase(
            response_language_mode,
            en=f"Summary: As of {date.today().isoformat()}.",
            np=f"Summary: {date.today().isoformat()} samma.",
            ne=f"सारांश: {date.today().isoformat()} सम्म।",
        )
    )
    return "\n".join(lines)


def validate_business_profile_ownership(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            select id
            from public.profiles
            where id = %(profile_id)s::uuid
              and user_id = %(user_id)s::uuid
              and profile_type = 'business'
            limit 1
            """,
            {"profile_id": profile_id, "user_id": user_id},
        )
        row = cur.fetchone()
    if not row:
        raise ApiError(
            status_code=403,
            code="invalid_business_profile",
            message="Provided profile_id is not an owned business profile.",
        )


@dataclass(frozen=True)
class BusinessToolContext:
    route_label: str
    mode: str
    warnings: list[str]
    direct_reply: str | None
    usage: dict | None
    accounting_context: dict | None
    snapshot: dict | None
    matches: list[dict]


def build_business_tool_context(
    conn: Connection,
    *,
    settings: Settings,
    user_id: str,
    profile_id: str,
    user_query: str,
) -> BusinessToolContext:
    validate_business_profile_ownership(conn, user_id=user_id, profile_id=profile_id)
    response_language_mode = detect_response_language_mode(user_query)
    route = route_business_chat_intent(user_query)
    manage_category_reply = _build_business_manage_category_reply(
        conn,
        user_id=user_id,
        profile_id=profile_id,
        query=user_query,
        response_language_mode=response_language_mode,
    )
    if manage_category_reply:
        return BusinessToolContext(
            route_label=route.label,
            mode=route.mode,
            warnings=[],
            direct_reply=format_business_reply_like_personal(manage_category_reply),
            usage={"route": "deterministic", "intent": "manage_category_lookup"},
            accounting_context=None,
            snapshot=None,
            matches=[],
        )

    deterministic = try_generate_deterministic_business_response(
        conn,
        user_id=user_id,
        profile_id=profile_id,
        user_query=user_query,
    )

    if deterministic.handled and deterministic.reply:
        usage = {
            "route": deterministic.route,
            "intent": deterministic.intent,
        }
        if deterministic.entity_type:
            usage["entity_type"] = deterministic.entity_type
        if deterministic.entity_match_confidence is not None:
            usage["entity_match_confidence"] = float(deterministic.entity_match_confidence)
        if deterministic.resolution_status:
            usage["resolution_status"] = deterministic.resolution_status
        return BusinessToolContext(
            route_label=route.label,
            mode=route.mode,
            warnings=deterministic.warnings or [],
            direct_reply=format_business_reply_like_personal(deterministic.reply),
            usage=usage,
            accounting_context=None,
            snapshot=None,
            matches=[],
        )

    understanding = parse_business_query_understanding(user_query)
    if understanding.intent in _INVENTORY_ONLY_INTENTS:
        usage = {
            "route": "inventory_only_guard",
            "intent": understanding.intent,
        }
        if deterministic.entity_type:
            usage["entity_type"] = deterministic.entity_type
        if deterministic.entity_match_confidence is not None:
            usage["entity_match_confidence"] = float(deterministic.entity_match_confidence)
        if deterministic.resolution_status:
            usage["resolution_status"] = deterministic.resolution_status
        return BusinessToolContext(
            route_label=route.label,
            mode=route.mode,
            warnings=deterministic.warnings or [],
            direct_reply=format_business_reply_like_personal(
                deterministic.reply
                or (
                    "I could not match that stock question to your inventory page data. Please ask with the product name exactly as it appears in inventory."
                    if response_language_mode == "english"
                    else (
                        "Tapai ko stock question lai inventory page ko data sanga match garna sakina. Inventory ma jastai product name cha tesai le sodhnuhos."
                        if response_language_mode == "neplish"
                        else "तपाईंको स्टक प्रश्नलाई इन्भेन्टरी पेजको डाटासँग मिलाउन सकिएन। इन्भेन्टरीमा जस्तै प्रोडक्ट नाम छ त्यही अनुसार सोध्नुहोस्।"
                    )
                )
            ),
            usage=usage,
            accounting_context=None,
            snapshot=None,
            matches=[],
        )

    snapshot = collect_business_live_snapshot(
        conn,
        user_id=user_id,
        profile_id=profile_id,
    )
    if isinstance(snapshot, dict):
        snapshot = {
            **snapshot,
            "normalized_query": understanding.normalized_query,
        }
    vector_context = get_business_vector_context(
        conn,
        settings=settings,
        user_id=user_id,
        profile_id=profile_id,
        query=user_query,
    )
    accounting_context = build_business_accounting_context(
        conn,
        user_id=user_id,
        profile_id=profile_id,
        query=user_query,
        scope=understanding.scope,
    )

    warnings = [*vector_context.warnings, *(deterministic.warnings or [])]
    usage = {
        "route": "llm_fallback",
        "intent": deterministic.intent,
    }
    if deterministic.entity_type:
        usage["entity_type"] = deterministic.entity_type
    if deterministic.entity_match_confidence is not None:
        usage["entity_match_confidence"] = float(deterministic.entity_match_confidence)
    if deterministic.resolution_status:
        usage["resolution_status"] = deterministic.resolution_status

    if understanding.intent == "party_transactions" and not understanding.entity_text_candidates:
        generic_history_reply = _build_generic_business_history_reply(
            accounting_context,
            scope_label=understanding.scope.label if understanding.scope else None,
            response_language_mode=response_language_mode,
        )
        if generic_history_reply:
            return BusinessToolContext(
                route_label=route.label,
                mode=route.mode,
                warnings=warnings,
                direct_reply=format_business_reply_like_personal(generic_history_reply),
                usage=usage,
                accounting_context=None,
                snapshot=None,
                matches=[],
            )

    return BusinessToolContext(
        route_label=route.label,
        mode=route.mode,
        warnings=warnings,
        direct_reply=None,
        usage=usage,
        accounting_context=accounting_context,
        snapshot=snapshot,
        matches=vector_context.matches,
    )
