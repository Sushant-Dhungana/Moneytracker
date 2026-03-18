import httpx

from app.core.config import Settings
from app.core.errors import ApiError


def _build_headers(settings: Settings, *, bearer: str | None = None) -> dict[str, str]:
    headers = {
        "apikey": settings.supabase_anon_key or "",
        "Content-Type": "application/json",
    }
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    return headers


def supabase_auth_post(
    settings: Settings,
    path: str,
    payload: dict,
    *,
    bearer: str | None = None,
) -> dict:
    url = f"{settings.supabase_issuer.rstrip('/')}/{path.lstrip('/')}"
    try:
        with httpx.Client(timeout=float(settings.supabase_auth_timeout_sec)) as client:
            response = client.post(
                url,
                headers=_build_headers(settings, bearer=bearer),
                json=payload,
            )
    except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout) as exc:  # pragma: no cover
        raise ApiError(
            status_code=503,
            code="supabase_auth_timeout",
            message=(
                "Supabase auth request timed out. Check laptop internet connectivity and "
                "SUPABASE_URL/SUPABASE_ANON_KEY in backend/.env."
            ),
        ) from exc
    except Exception as exc:  # pragma: no cover
        raise ApiError(
            status_code=503,
            code="supabase_auth_unreachable",
            message=f"Supabase auth unreachable: {exc}",
        ) from exc

    if response.is_success:
        return response.json() if response.content else {}

    error_payload: dict = {}
    try:
        error_payload = response.json()
    except Exception:
        pass
    message = (
        error_payload.get("msg")
        or error_payload.get("message")
        or error_payload.get("error_description")
        or error_payload.get("error")
        or response.text
        or f"Supabase auth error ({response.status_code})"
    )
    raise ApiError(
        status_code=response.status_code,
        code="supabase_auth_error",
        message=str(message),
    )
