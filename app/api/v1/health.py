from fastapi import APIRouter, Depends
from psycopg import Connection

from app.core.config import Settings, get_settings
from app.core.db import get_db_conn
from app.schemas.common import HealthResponse

router = APIRouter(prefix="/health", tags=["health"])


@router.get("", response_model=HealthResponse)
def health_check(settings: Settings = Depends(get_settings)) -> HealthResponse:
    return HealthResponse(status="ok", service=settings.app_name, env=settings.app_env)


@router.get("/db")
def health_check_db(conn: Connection = Depends(get_db_conn)) -> dict:
    with conn.cursor() as cur:
        cur.execute("select 1 as ok")
        row = cur.fetchone() or {}
    return {"status": "ok", "db": row.get("ok") == 1}
