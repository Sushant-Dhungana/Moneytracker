from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from psycopg import Connection

from app.core.auth import AuthContext, get_auth_context
from app.core.db import apply_db_auth_context, get_db_conn
from app.services.categories_service import (
    create_category,
    delete_category,
    ensure_default_categories,
    fetch_categories,
)

router = APIRouter(prefix="/categories", tags=["categories"])


class CategoryCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    type: str = Field(pattern="^(income|expense)$")
    parent_id: str | None = None
    icon: str | None = None
    color: str | None = None


@router.get("")
def get_categories(
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    apply_db_auth_context(conn, auth.user_id)
    return {"items": fetch_categories(conn, auth.user_id)}


@router.post("")
def post_category(
    payload: CategoryCreateRequest,
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    apply_db_auth_context(conn, auth.user_id)
    item = create_category(
        conn,
        user_id=auth.user_id,
        name=payload.name,
        cat_type=payload.type,
        parent_id=payload.parent_id,
        icon=payload.icon,
        color=payload.color,
    )
    return {"item": item}


@router.delete("/{category_id}")
def remove_category(
    category_id: str,
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    apply_db_auth_context(conn, auth.user_id)
    delete_category(conn, user_id=auth.user_id, category_id=category_id)
    return {"ok": True}


@router.post("/defaults")
def seed_default_categories(
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> dict:
    apply_db_auth_context(conn, auth.user_id)
    ensure_default_categories(conn, user_id=auth.user_id)
    return {"ok": True}

