from __future__ import annotations

from psycopg import Connection

from app.arthaxai.chat.chat_scopes import BUSINESS_SCOPE, PERSONAL_SCOPE, ChatScope
from app.arthaxai.chat.history import fetch_recent_messages
from app.arthaxai.chat.prompt_builder import build_business_prompt, build_personal_prompt
from app.arthaxai.chat.response_formatter import (
    format_business_reply_like_personal,
    format_personal_reply_like_business_structure,
)
from app.arthaxai.chat.transport import request_chat_completion
from app.arthaxai.tools.business_tools import build_business_tool_context
from app.arthaxai.tools.personal_tools import build_personal_tool_context
from app.core.config import Settings
from app.core.errors import ApiError


def _fetch_recent_history_text(
    conn: Connection,
    *,
    user_id: str,
    scope: str,
    thread_id: str,
    limit: int = 10,
) -> str:
    rows = fetch_recent_messages(
        conn,
        user_id=user_id,
        scope=scope,
        thread_id=thread_id,
        limit=limit,
    )
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


def generate_chat_reply(
    conn: Connection,
    *,
    settings: Settings,
    scope: ChatScope,
    user_id: str,
    thread_id: str,
    user_query: str,
    profile_id: str | None = None,
) -> tuple[str, dict | None, list[str]]:
    if scope == PERSONAL_SCOPE:
        context = build_personal_tool_context(
            conn,
            settings=settings,
            user_id=user_id,
            user_query=user_query,
        )
        if context.direct_reply:
            usage = {"route": context.route_label, "mode": context.mode}
            return context.direct_reply, usage, context.warnings

        recent_history = _fetch_recent_history_text(
            conn,
            user_id=user_id,
            scope=scope,
            thread_id=thread_id,
        )
        prompt = build_personal_prompt(
            user_query=user_query,
            recent_history=recent_history,
            evidence=context.evidence or {},
        )
        reply, usage, upstream_warnings = request_chat_completion(
            scope=scope,
            prompt=prompt,
            settings=settings,
        )
        formatted_reply = format_personal_reply_like_business_structure(reply)
        usage_payload = dict(usage) if isinstance(usage, dict) else {}
        usage_payload.setdefault("route", context.route_label)
        usage_payload.setdefault("mode", context.mode)
        return formatted_reply, (usage_payload or None), [*context.warnings, *upstream_warnings]

    if scope == BUSINESS_SCOPE:
        normalized_profile_id = str(profile_id or "").strip()
        if not normalized_profile_id:
            raise ApiError(
                status_code=400,
                code="business_profile_required",
                message="profile_id is required for business chat.",
            )

        context = build_business_tool_context(
            conn,
            settings=settings,
            user_id=user_id,
            profile_id=normalized_profile_id,
            user_query=user_query,
        )
        if context.direct_reply:
            return context.direct_reply, context.usage, context.warnings

        recent_history = _fetch_recent_history_text(
            conn,
            user_id=user_id,
            scope=scope,
            thread_id=thread_id,
        )
        prompt = build_business_prompt(
            user_query=user_query,
            recent_history=recent_history,
            accounting_context=context.accounting_context or {},
            snapshot=context.snapshot or {},
            matches=context.matches,
        )
        reply, usage, upstream_warnings = request_chat_completion(
            scope=scope,
            prompt=prompt,
            settings=settings,
        )
        formatted_reply = format_business_reply_like_personal(reply)
        usage_payload = dict(context.usage) if isinstance(context.usage, dict) else {}
        if isinstance(usage, dict):
            usage_payload.update(usage)
        usage_payload.setdefault("route", context.route_label)
        usage_payload.setdefault("mode", context.mode)
        return formatted_reply, (usage_payload or None), [*context.warnings, *upstream_warnings]

    raise ApiError(status_code=400, code="invalid_chat_scope", message="Invalid chat scope.")
