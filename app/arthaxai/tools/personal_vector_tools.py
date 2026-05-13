from __future__ import annotations

from dataclasses import dataclass

from psycopg import Connection

from app.core.config import Settings
from app.arthaxai.services.ai_personal_vector_service import (
    get_personal_vector_status_warnings,
    retrieve_personal_vector_matches,
)


@dataclass(frozen=True)
class PersonalVectorContext:
    warnings: list[str]
    matches: list[dict]


def get_personal_vector_context(
    conn: Connection,
    *,
    settings: Settings,
    user_id: str,
    profile_id: str,
    query: str,
) -> PersonalVectorContext:
    warnings = get_personal_vector_status_warnings(
        conn,
        user_id=user_id,
        profile_id=profile_id,
    )
    matches = retrieve_personal_vector_matches(
        conn,
        settings=settings,
        user_id=user_id,
        profile_id=profile_id,
        query=query,
        match_threshold=settings.personal_chat_match_threshold,
        match_count=settings.personal_chat_match_count,
    )
    return PersonalVectorContext(warnings=warnings, matches=matches)
