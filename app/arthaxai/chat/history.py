from psycopg import Connection
from psycopg.errors import InvalidTextRepresentation, UndefinedColumn, UndefinedTable

from app.core.errors import ApiError

ChatScope = str
VALID_SCOPES = {"personal", "business"}
AI_HISTORY_MISSING_SCHEMA_MESSAGE = (
    "AI chat history tables are missing. Apply Supabase migrations "
    "'202603090001_ai_chat_history_and_business_vectors.sql' and "
    "'202603090003_business_chat_history_tables.sql' "
    "to the active database and retry."
)
_HISTORY_TABLE_CANDIDATES_BY_SCOPE: dict[ChatScope, list[tuple[str, str]]] = {
    # Business chat history must live in business schema.
    "business": [
        ("business.chat_threads", "business.chat_messages"),
        ("business.ai_chat_threads", "business.ai_chat_messages"),
    ],
    # Personal history is also stored in business schema for strict AI data separation.
    "personal": [
        ("business.chat_threads", "business.chat_messages"),
        ("business.ai_chat_threads", "business.ai_chat_messages"),
    ],
}


def _translate_history_db_error(exc: Exception) -> ApiError:
    if isinstance(exc, (UndefinedTable, UndefinedColumn)):
        return ApiError(
            status_code=500,
            code="ai_history_schema_missing",
            message=AI_HISTORY_MISSING_SCHEMA_MESSAGE,
        )
    if isinstance(exc, InvalidTextRepresentation):
        return ApiError(
            status_code=400,
            code="invalid_thread_id",
            message="Invalid chat thread id.",
        )
    return ApiError(
        status_code=500,
        code="ai_history_error",
        message="Failed to access AI chat history.",
    )


def _relation_exists(conn: Connection, relation: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("select to_regclass(%(relation)s) as rel", {"relation": relation})
        row = cur.fetchone() or {}
    return bool(row.get("rel"))


def _resolve_history_tables(conn: Connection, scope: ChatScope) -> tuple[str, str]:
    if scope not in VALID_SCOPES:
        raise ApiError(status_code=400, code="invalid_chat_scope", message="Invalid chat scope.")

    candidates = _HISTORY_TABLE_CANDIDATES_BY_SCOPE.get(scope, [])
    for threads_table, messages_table in candidates:
        if _relation_exists(conn, threads_table) and _relation_exists(conn, messages_table):
            return threads_table, messages_table

    raise ApiError(
        status_code=500,
        code="ai_history_schema_missing",
        message=AI_HISTORY_MISSING_SCHEMA_MESSAGE,
    )


def _normalize_scope_profile(scope: ChatScope, profile_id: str | None) -> str | None:
    if scope not in VALID_SCOPES:
        raise ApiError(status_code=400, code="invalid_chat_scope", message="Invalid chat scope.")

    normalized_profile_id = str(profile_id).strip() if profile_id else None
    if scope == "business" and not normalized_profile_id:
        raise ApiError(
            status_code=400,
            code="business_profile_required",
            message="profile_id is required for business chat.",
        )
    if scope == "personal":
        return None
    return normalized_profile_id


def _assert_thread_scope(
    row: dict,
    *,
    scope: ChatScope,
    profile_id: str | None,
) -> None:
    row_scope = str(row.get("scope") or "").strip()
    row_profile_id = str(row.get("profile_id") or "").strip() or None
    if row_scope != scope:
        raise ApiError(
            status_code=400,
            code="chat_scope_mismatch",
            message="Provided thread does not belong to this chat scope.",
        )
    if row_profile_id != profile_id:
        raise ApiError(
            status_code=400,
            code="chat_profile_mismatch",
            message="Provided thread does not belong to this profile.",
        )


def _load_thread_row(
    conn: Connection,
    *,
    threads_table: str,
    user_id: str,
    thread_id: str,
) -> dict:
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                select id::text as id, scope, profile_id::text as profile_id
                from {threads_table}
                where id = %(thread_id)s::uuid
                  and user_id = %(user_id)s::uuid
                limit 1
                """,
                {"thread_id": thread_id, "user_id": user_id},
            )
            row = cur.fetchone() or {}
    except Exception as exc:
        raise _translate_history_db_error(exc) from exc

    if not row:
        raise ApiError(
            status_code=404,
            code="thread_not_found",
            message="Chat thread not found.",
        )
    return row


def ensure_chat_thread(
    conn: Connection,
    *,
    user_id: str,
    scope: ChatScope,
    profile_id: str | None,
    thread_id: str | None,
    title_hint: str | None = None,
) -> str:
    normalized_profile_id = _normalize_scope_profile(scope, profile_id)

    threads_table, _ = _resolve_history_tables(conn, scope)

    if thread_id:
        row = _load_thread_row(
            conn,
            threads_table=threads_table,
            user_id=user_id,
            thread_id=thread_id,
        )
        _assert_thread_scope(row, scope=scope, profile_id=normalized_profile_id)
        return str(row.get("id") or "").strip()

    initial_title = (title_hint or "").strip()
    if not initial_title:
        initial_title = "Chat"
    if len(initial_title) > 120:
        initial_title = f"{initial_title[:117]}..."

    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                insert into {threads_table} (
                  user_id, scope, profile_id, title, created_at, updated_at
                )
                values (
                  %(user_id)s::uuid,
                  %(scope)s::text,
                  %(profile_id)s::uuid,
                  %(title)s::text,
                  now(),
                  now()
                )
                returning id::text as id
                """,
                {
                    "user_id": user_id,
                    "scope": scope,
                    "profile_id": normalized_profile_id,
                    "title": initial_title,
                },
            )
            row = cur.fetchone() or {}
    except Exception as exc:
        raise _translate_history_db_error(exc) from exc

    next_thread_id = str(row.get("id") or "").strip()
    if not next_thread_id:
        raise ApiError(
            status_code=500,
            code="thread_create_failed",
            message="Failed to create chat thread.",
        )
    return next_thread_id


def append_chat_message(
    conn: Connection,
    *,
    user_id: str,
    scope: ChatScope,
    thread_id: str,
    role: str,
    content: str,
) -> None:
    threads_table, messages_table = _resolve_history_tables(conn, scope)
    normalized_content = str(content or "").strip()
    if not normalized_content:
        return

    normalized_role = str(role or "").strip().lower()
    if normalized_role not in {"user", "assistant", "system"}:
        raise ApiError(
            status_code=400,
            code="invalid_chat_role",
            message="Invalid chat role.",
        )

    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                insert into {messages_table} (
                  thread_id, user_id, role, content, created_at
                ) values (
                  %(thread_id)s::uuid,
                  %(user_id)s::uuid,
                  %(role)s::text,
                  %(content)s::text,
                  now()
                )
                """,
                {
                    "thread_id": thread_id,
                    "user_id": user_id,
                    "role": normalized_role,
                    "content": normalized_content,
                },
            )
            cur.execute(
                f"""
                update {threads_table}
                set updated_at = now()
                where id = %(thread_id)s::uuid
                  and user_id = %(user_id)s::uuid
                """,
                {"thread_id": thread_id, "user_id": user_id},
            )
    except Exception as exc:
        raise _translate_history_db_error(exc) from exc


def fetch_recent_messages(
    conn: Connection,
    *,
    user_id: str,
    scope: ChatScope,
    thread_id: str,
    limit: int = 12,
) -> list[dict]:
    _, messages_table = _resolve_history_tables(conn, scope)
    safe_limit = max(1, min(int(limit or 12), 40))
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                select role, content, created_at::text as created_at
                from {messages_table}
                where thread_id = %(thread_id)s::uuid
                  and user_id = %(user_id)s::uuid
                order by created_at desc
                limit %(limit)s
                """,
                {
                    "thread_id": thread_id,
                    "user_id": user_id,
                    "limit": safe_limit,
                },
            )
            rows = cur.fetchall() or []
    except Exception as exc:
        raise _translate_history_db_error(exc) from exc

    return list(reversed(rows))


def list_chat_threads(
    conn: Connection,
    *,
    user_id: str,
    scope: ChatScope,
    profile_id: str | None,
    limit: int = 20,
) -> list[dict]:
    normalized_profile_id = _normalize_scope_profile(scope, profile_id)
    threads_table, messages_table = _resolve_history_tables(conn, scope)
    safe_limit = max(1, min(int(limit or 20), 100))

    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                select
                  t.id::text as thread_id,
                  t.scope,
                  t.profile_id::text as profile_id,
                  t.title,
                  t.created_at::text as created_at,
                  t.updated_at::text as updated_at,
                  coalesce(mc.message_count, 0)::int as message_count
                from {threads_table} t
                left join lateral (
                  select count(*)::int as message_count
                  from {messages_table} m
                  where m.thread_id = t.id
                    and m.user_id = %(user_id)s::uuid
                ) mc on true
                where t.user_id = %(user_id)s::uuid
                  and t.scope = %(scope)s::text
                  and (
                    (%(scope)s = 'personal' and t.profile_id is null)
                    or (%(scope)s = 'business' and t.profile_id = %(profile_id)s::uuid)
                  )
                order by t.updated_at desc, t.created_at desc
                limit %(limit)s
                """,
                {
                    "user_id": user_id,
                    "scope": scope,
                    "profile_id": normalized_profile_id,
                    "limit": safe_limit,
                },
            )
            rows = cur.fetchall() or []
    except Exception as exc:
        raise _translate_history_db_error(exc) from exc

    return rows


def fetch_chat_messages(
    conn: Connection,
    *,
    user_id: str,
    scope: ChatScope,
    profile_id: str | None,
    thread_id: str,
    limit: int = 120,
    before_id: int | None = None,
    latest: bool = False,
) -> list[dict]:
    normalized_profile_id = _normalize_scope_profile(scope, profile_id)
    threads_table, messages_table = _resolve_history_tables(conn, scope)
    safe_limit = max(1, min(int(limit or 120), 400))
    safe_before_id = int(before_id) if before_id and int(before_id) > 0 else None

    row = _load_thread_row(
        conn,
        threads_table=threads_table,
        user_id=user_id,
        thread_id=thread_id,
    )
    _assert_thread_scope(row, scope=scope, profile_id=normalized_profile_id)

    try:
        with conn.cursor() as cur:
            if latest:
                before_clause = "and id < %(before_id)s::bigint" if safe_before_id else ""
                bind = {
                    "thread_id": thread_id,
                    "user_id": user_id,
                    "limit": safe_limit,
                }
                if safe_before_id:
                    bind["before_id"] = safe_before_id

                cur.execute(
                    f"""
                    select
                      id,
                      role,
                      content,
                      created_at::text as created_at
                    from {messages_table}
                    where thread_id = %(thread_id)s::uuid
                      and user_id = %(user_id)s::uuid
                      {before_clause}
                    order by created_at desc, id desc
                    limit %(limit)s
                    """,
                    bind,
                )
                rows = cur.fetchall() or []
                rows.reverse()
                return rows

            before_clause = "and id < %(before_id)s::bigint" if safe_before_id else ""
            bind = {
                "thread_id": thread_id,
                "user_id": user_id,
                "limit": safe_limit,
            }
            if safe_before_id:
                bind["before_id"] = safe_before_id

            cur.execute(
                f"""
                select
                  id,
                  role,
                  content,
                  created_at::text as created_at
                from {messages_table}
                where thread_id = %(thread_id)s::uuid
                  and user_id = %(user_id)s::uuid
                  {before_clause}
                order by created_at asc, id asc
                limit %(limit)s
                """,
                bind,
            )
            rows = cur.fetchall() or []
    except Exception as exc:
        raise _translate_history_db_error(exc) from exc

    return rows
