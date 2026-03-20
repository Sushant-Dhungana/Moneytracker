from typing import Any

from fastapi import APIRouter, Depends

from app.core.auth import AuthContext, get_auth_context
from app.core.config import Settings, get_settings
from app.schemas.receipt import ReceiptProxyRequest
from app.services.receipt_proxy_service import proxy_receipt_request

router = APIRouter(prefix="/receipt", tags=["receipt"])


@router.post("", response_model=dict[str, Any])
@router.post("/", response_model=dict[str, Any], include_in_schema=False)
def proxy_receipt(
    payload: ReceiptProxyRequest,
    _: AuthContext = Depends(get_auth_context),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    return proxy_receipt_request(
        target=payload.target,
        payload=payload.payload,
        settings=settings,
    )
