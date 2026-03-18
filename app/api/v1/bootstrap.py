from fastapi import APIRouter, Depends
from psycopg import Connection

from app.core.auth import AuthContext, get_auth_context
from app.core.db import apply_db_auth_context, get_db_conn
from app.schemas.bootstrap import MobileBootstrapResponse
from app.services.bootstrap_service import fetch_mobile_bootstrap

router = APIRouter(prefix="/bootstrap", tags=["bootstrap"])


@router.get("/mobile", response_model=MobileBootstrapResponse)
def get_mobile_bootstrap(
    auth: AuthContext = Depends(get_auth_context),
    conn: Connection = Depends(get_db_conn),
) -> MobileBootstrapResponse:
    apply_db_auth_context(conn, auth.user_id)
    payload = fetch_mobile_bootstrap(conn, user_id=auth.user_id, email=auth.email)
    return MobileBootstrapResponse(**payload)
