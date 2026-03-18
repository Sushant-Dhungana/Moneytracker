from dataclasses import dataclass
from functools import lru_cache
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import jwt
from jwt.exceptions import PyJWKClientConnectionError
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import Settings, get_settings
from app.core.errors import ApiError


security = HTTPBearer(auto_error=False)


@dataclass
class AuthContext:
    user_id: str
    email: str | None
    claims: dict
    access_token: str


@lru_cache(maxsize=1)
def _jwks_client(jwks_url: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(jwks_url)


def _extract_token(credentials: HTTPAuthorizationCredentials | None) -> str:
    if not credentials or not credentials.credentials:
        raise ApiError(status_code=401, code="unauthorized", message="Missing bearer token.")
    return credentials.credentials


def _decode_access_token(token: str, settings: Settings) -> dict:
    try:
        signing_key = _jwks_client(settings.supabase_jwks_url).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["ES256", "RS256"],
            audience=settings.supabase_jwt_aud,
            issuer=settings.supabase_issuer,
        )
        return claims
    except PyJWKClientConnectionError as exc:
        raise ApiError(
            status_code=503,
            code="jwks_unreachable",
            message=(
                f"Cannot reach Supabase JWKS at {settings.supabase_jwks_url}. "
                "Check SUPABASE_URL in backend/.env."
            ),
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise ApiError(status_code=401, code="invalid_token", message="Invalid access token.") from exc


def _validate_token_via_supabase_user(token: str, settings: Settings) -> dict | None:
    user_url = f"{settings.supabase_issuer}/user"
    headers = {"Authorization": f"Bearer {token}"}
    if settings.supabase_anon_key:
        headers["apikey"] = settings.supabase_anon_key

    request = Request(user_url, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8"))
            if isinstance(payload, dict) and payload.get("id"):
                return {
                    "sub": str(payload["id"]),
                    "email": payload.get("email"),
                    "source": "supabase_user_endpoint",
                }
            return None
    except HTTPError:
        return None
    except URLError as exc:
        raise ApiError(
            status_code=503,
            code="supabase_auth_unreachable",
            message="Cannot reach Supabase auth user endpoint for token validation.",
        ) from exc


def get_auth_context(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    settings: Settings = Depends(get_settings),
) -> AuthContext:
    token = _extract_token(credentials)
    try:
        claims = _decode_access_token(token, settings)
    except ApiError as exc:
        fallback_claims = _validate_token_via_supabase_user(token, settings)
        if not fallback_claims:
            raise
        claims = fallback_claims

    user_id = claims.get("sub")
    if not user_id:
        raise ApiError(status_code=401, code="invalid_token", message="Token subject is missing.")

    email = claims.get("email")
    return AuthContext(
        user_id=str(user_id),
        email=str(email) if isinstance(email, str) and email.strip() else None,
        claims=claims,
        access_token=token,
    )
