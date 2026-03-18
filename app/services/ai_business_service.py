import json

from psycopg import Connection

from app.core.config import Settings
from app.core.errors import ApiError
from app.services.ai_business_query_service import (
    try_generate_deterministic_business_response,
)
from app.services.ai_business_vector_service import (
    collect_business_live_snapshot,
    get_business_vector_status_warnings,
    retrieve_business_vector_matches,
)
from app.services.ai_history_service import fetch_recent_messages
from app.services.ai_transport_service import request_chat_completion



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



def _build_recent_history_text(rows: list[dict]) -> str:
    if not rows:
        return ""
    lines: list[str] = []
    for row in rows[-12:]:
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



def _build_business_prompt(
    *,
    user_query: str,
    recent_history: str,
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
        "snapshot": snapshot,
        "retrieved_business_facts": compact_matches,
    }

    return (
        "You are arthaX Business AI assistant.\n"
        "Hard rules:\n"
        "1) Use only BUSINESS evidence below. Never use personal profile assumptions.\n"
        "2) SQL snapshot fields are authoritative for every exact number, including party-wise dues.\n"
        "3) Retrieved vector facts are contextual support only; do not use them as final numeric truth.\n"
        "4) Keep receivable/payable settlements separate from fresh income/expense when explaining.\n"
        "5) If data is missing or conflicting, explicitly say what is missing and suggest the next action.\n"
        "6) Never guess missing amounts. Use NPR for money.\n\n"
        "7) For customer/supplier-specific due questions, first check snapshot.customer_due_breakdown / "
        "snapshot.supplier_due_breakdown and match names case-insensitively.\n\n"
        f"User query:\n{user_query.strip()}\n\n"
        f"Recent conversation:\n{recent_history or '[none]'}\n\n"
        "Business evidence JSON:\n"
        f"{json.dumps(evidence_payload, ensure_ascii=True)}\n\n"
        "Return a clear business-focused answer.\n"
        "If you include any number, it must be grounded in SQL snapshot values from evidence JSON."
    )



def generate_business_chat_reply(
    conn: Connection,
    *,
    settings: Settings,
    user_id: str,
    profile_id: str,
    thread_id: str,
    user_query: str,
) -> tuple[str, dict | None, list[str]]:
    validate_business_profile_ownership(conn, user_id=user_id, profile_id=profile_id)

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
        return deterministic.reply, usage, deterministic.warnings or []

    warnings = get_business_vector_status_warnings(
        conn,
        user_id=user_id,
        profile_id=profile_id,
    )

    snapshot = collect_business_live_snapshot(
        conn,
        user_id=user_id,
        profile_id=profile_id,
    )

    matches = retrieve_business_vector_matches(
        conn,
        settings=settings,
        user_id=user_id,
        profile_id=profile_id,
        query=user_query,
        match_threshold=settings.business_chat_match_threshold,
        match_count=settings.business_chat_match_count,
    )

    history_rows = fetch_recent_messages(
        conn,
        user_id=user_id,
        scope="business",
        thread_id=thread_id,
        limit=10,
    )
    recent_history = _build_recent_history_text(history_rows)

    prompt = _build_business_prompt(
        user_query=user_query,
        recent_history=recent_history,
        snapshot=snapshot,
        matches=matches,
    )

    reply, usage, upstream_warnings = request_chat_completion(
        scope="business",
        prompt=prompt,
        settings=settings,
    )

    usage_payload = dict(usage) if isinstance(usage, dict) else {}
    usage_payload["route"] = "llm_fallback"
    usage_payload["intent"] = deterministic.intent
    if deterministic.entity_type:
        usage_payload["entity_type"] = deterministic.entity_type
    if deterministic.entity_match_confidence is not None:
        usage_payload["entity_match_confidence"] = float(deterministic.entity_match_confidence)
    if deterministic.resolution_status:
        usage_payload["resolution_status"] = deterministic.resolution_status

    return reply, (usage_payload or None), [*warnings, *upstream_warnings, *(deterministic.warnings or [])]
