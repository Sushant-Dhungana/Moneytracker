from __future__ import annotations

from dataclasses import dataclass

from psycopg import Connection

from app.core.config import Settings
from app.arthaxai.services.ai_business_vector_service import (
    get_business_vector_status_warnings,
    retrieve_business_vector_matches,
)


@dataclass(frozen=True)
class BusinessVectorContext:
    warnings: list[str]
    matches: list[dict]


def get_business_vector_context(
    conn: Connection,
    *,
    settings: Settings,
    user_id: str,
    profile_id: str,
    query: str,
) -> BusinessVectorContext:
    warnings = get_business_vector_status_warnings(
        conn,
        user_id=user_id,
        profile_id=profile_id,
    )
    matches = retrieve_business_vector_matches(
        conn,
        settings=settings,
        user_id=user_id,
        profile_id=profile_id,
        query=query,
        match_threshold=settings.business_chat_match_threshold,
        match_count=settings.business_chat_match_count,
    )
    return BusinessVectorContext(warnings=warnings, matches=matches)
