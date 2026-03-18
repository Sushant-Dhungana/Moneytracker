import json
from collections.abc import Generator

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from psycopg import Connection

from app.core.auth import AuthContext, get_auth_context
from app.core.config import Settings, get_settings
from app.core.db import apply_db_auth_context, get_db_conn
from app.schemas.ai import (
    BusinessChatRequest,
    ChatMessagesResponse,
    ChatResponse,
    ChatThreadsResponse,
    PersonalChatRequest,
)
from app.services.ai_business_service import generate_business_chat_reply
from app.services.ai_history_service import (
    append_chat_message,
    ensure_chat_thread,
    fetch_chat_messages,
    list_chat_threads,
)
from app.services.ai_personal_service import generate_personal_chat_reply

router = APIRouter(prefix="/ai", tags=["ai"])



def _stream_text_sse(
    *,
    thread_id: str,
    reply: str,
    warnings: list[str],
    chunk_size: int = 18,
) -> Generator[str, None, None]:
    meta_payload = {"thread_id": thread_id, "warnings": warnings or []}
    yield f"data: {json.dumps(meta_payload, ensure_ascii=True)}\n\n"

    text = reply or ""
    for start in range(0, len(text), max(1, chunk_size)):
        token = text[start : start + max(1, chunk_size)]
        payload = {"delta": token}
        yield f"data: {json.dumps(payload, ensure_ascii=True)}\n\n"

    yield "data: [DONE]\n\n"


@router.post("/personal/chat", response_model=ChatResponse)
def post_personal_chat(
    payload: PersonalChatRequest,
    auth: AuthContext = Depends(get_auth_context),
    settings: Settings = Depends(get_settings),
    conn: Connection = Depends(get_db_conn),
):
    apply_db_auth_context(conn, auth.user_id)

    thread_id = ensure_chat_thread(
        conn,
        user_id=auth.user_id,
        scope="personal",
        profile_id=None,
        thread_id=payload.thread_id,
        title_hint=payload.message,
    )
    append_chat_message(
        conn,
        user_id=auth.user_id,
        scope="personal",
        thread_id=thread_id,
        role="user",
        content=payload.message,
    )

    reply, usage, warnings = generate_personal_chat_reply(
        conn,
        settings=settings,
        user_id=auth.user_id,
        thread_id=thread_id,
        user_query=payload.message,
    )

    append_chat_message(
        conn,
        user_id=auth.user_id,
        scope="personal",
        thread_id=thread_id,
        role="assistant",
        content=reply,
    )

    if payload.stream:
        return StreamingResponse(
            _stream_text_sse(thread_id=thread_id, reply=reply, warnings=warnings),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "x-thread-id": thread_id,
            },
        )

    return ChatResponse(
        thread_id=thread_id,
        reply=reply,
        streamed=False,
        usage=usage,
        warnings=warnings or None,
    )


@router.get("/personal/threads", response_model=ChatThreadsResponse)
def get_personal_threads(
    limit: int = Query(default=20, ge=1, le=100),
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
):
    apply_db_auth_context(conn, auth.user_id)
    items = list_chat_threads(
        conn,
        user_id=auth.user_id,
        scope="personal",
        profile_id=None,
        limit=limit,
    )
    return ChatThreadsResponse(items=items)


@router.get("/personal/messages", response_model=ChatMessagesResponse)
def get_personal_messages(
    thread_id: str = Query(min_length=1),
    limit: int = Query(default=120, ge=1, le=400),
    before_id: int | None = Query(default=None, ge=1),
    latest: bool = Query(default=False),
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
):
    apply_db_auth_context(conn, auth.user_id)
    normalized_thread_id = thread_id.strip()
    items = fetch_chat_messages(
        conn,
        user_id=auth.user_id,
        scope="personal",
        profile_id=None,
        thread_id=normalized_thread_id,
        limit=limit,
        before_id=before_id,
        latest=latest,
    )
    return ChatMessagesResponse(thread_id=normalized_thread_id, items=items)


@router.post("/business/chat", response_model=ChatResponse)
def post_business_chat(
    payload: BusinessChatRequest,
    auth: AuthContext = Depends(get_auth_context),
    settings: Settings = Depends(get_settings),
    conn: Connection = Depends(get_db_conn),
):
    apply_db_auth_context(conn, auth.user_id)

    profile_id = payload.profile_id.strip()
    thread_id = ensure_chat_thread(
        conn,
        user_id=auth.user_id,
        scope="business",
        profile_id=profile_id,
        thread_id=payload.thread_id,
        title_hint=payload.message,
    )
    append_chat_message(
        conn,
        user_id=auth.user_id,
        scope="business",
        thread_id=thread_id,
        role="user",
        content=payload.message,
    )

    reply, usage, warnings = generate_business_chat_reply(
        conn,
        settings=settings,
        user_id=auth.user_id,
        profile_id=profile_id,
        thread_id=thread_id,
        user_query=payload.message,
    )

    append_chat_message(
        conn,
        user_id=auth.user_id,
        scope="business",
        thread_id=thread_id,
        role="assistant",
        content=reply,
    )

    if payload.stream:
        return StreamingResponse(
            _stream_text_sse(thread_id=thread_id, reply=reply, warnings=warnings),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "x-thread-id": thread_id,
            },
        )

    return ChatResponse(
        thread_id=thread_id,
        reply=reply,
        streamed=False,
        usage=usage,
        warnings=warnings or None,
    )


@router.get("/business/threads", response_model=ChatThreadsResponse)
def get_business_threads(
    profile_id: str = Query(min_length=1),
    limit: int = Query(default=20, ge=1, le=100),
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
):
    apply_db_auth_context(conn, auth.user_id)
    normalized_profile_id = profile_id.strip()
    items = list_chat_threads(
        conn,
        user_id=auth.user_id,
        scope="business",
        profile_id=normalized_profile_id,
        limit=limit,
    )
    return ChatThreadsResponse(items=items)


@router.get("/business/messages", response_model=ChatMessagesResponse)
def get_business_messages(
    profile_id: str = Query(min_length=1),
    thread_id: str = Query(min_length=1),
    limit: int = Query(default=120, ge=1, le=400),
    before_id: int | None = Query(default=None, ge=1),
    latest: bool = Query(default=False),
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
):
    apply_db_auth_context(conn, auth.user_id)
    normalized_profile_id = profile_id.strip()
    normalized_thread_id = thread_id.strip()
    items = fetch_chat_messages(
        conn,
        user_id=auth.user_id,
        scope="business",
        profile_id=normalized_profile_id,
        thread_id=normalized_thread_id,
        limit=limit,
        before_id=before_id,
        latest=latest,
    )
    return ChatMessagesResponse(thread_id=normalized_thread_id, items=items)
