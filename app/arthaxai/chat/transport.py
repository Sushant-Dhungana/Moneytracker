from dataclasses import dataclass

import httpx

from app.core.config import Settings
from app.core.errors import ApiError


@dataclass
class UpstreamConfig:
    endpoint: str
    api_key: str | None
    api_key_header: str


def _normalize_api_key_header_value(header_name: str, api_key: str) -> str:
    if header_name.lower() != "authorization":
        return api_key
    return api_key if api_key.lower().startswith("bearer ") else f"Bearer {api_key}"


def resolve_upstream_config(scope: str, settings: Settings) -> UpstreamConfig:
    scope_key = scope.strip().lower()
    if scope_key not in {"personal", "business"}:
        raise ApiError(status_code=400, code="invalid_chat_scope", message="Invalid chat scope.")

    if scope_key == "personal":
        endpoint = (
            settings.personal_chat_api_endpoint
            or settings.chat_api_endpoint
            or settings.personal_chat_api_fallback_endpoint
        )
        api_key = settings.personal_chat_api_key or settings.chat_api_key
        api_key_header = (
            settings.personal_chat_api_key_header
            or settings.chat_api_key_header
            or "x-api-key"
        )
    else:
        endpoint = (
            settings.business_chat_api_endpoint
            or settings.chat_api_endpoint
            or settings.business_chat_api_fallback_endpoint
        )
        api_key = settings.business_chat_api_key or settings.chat_api_key
        api_key_header = (
            settings.business_chat_api_key_header
            or settings.chat_api_key_header
            or "x-api-key"
        )

    normalized_endpoint = str(endpoint or "").strip()
    if not normalized_endpoint:
        raise ApiError(
            status_code=500,
            code="chat_endpoint_missing",
            message=(
                f"{scope_key.capitalize()} chat upstream endpoint is not configured. "
                "Set PERSONAL_CHAT_API_ENDPOINT / BUSINESS_CHAT_API_ENDPOINT (or CHAT_API_ENDPOINT as legacy fallback) on backend."
            ),
        )

    return UpstreamConfig(
        endpoint=normalized_endpoint,
        api_key=(str(api_key).strip() if api_key else None),
        api_key_header=str(api_key_header).strip() or "x-api-key",
    )


def extract_reply(payload: dict) -> str | None:
    direct_candidates = [
        payload.get("reply"),
        payload.get("message"),
        payload.get("data"),
        payload.get("text"),
        (payload.get("output") or {}).get("text") if isinstance(payload.get("output"), dict) else None,
    ]

    for value in direct_candidates:
        if isinstance(value, str) and value.strip():
            return value.strip()

    nested_candidates = [
        ((payload.get("data") or {}).get("reply") if isinstance(payload.get("data"), dict) else None),
        ((payload.get("data") or {}).get("message") if isinstance(payload.get("data"), dict) else None),
        ((payload.get("result") or {}).get("reply") if isinstance(payload.get("result"), dict) else None),
        ((payload.get("result") or {}).get("message") if isinstance(payload.get("result"), dict) else None),
    ]

    for value in nested_candidates:
        if isinstance(value, str) and value.strip():
            return value.strip()

    return None


def request_chat_completion(
    *,
    scope: str,
    prompt: str,
    settings: Settings,
) -> tuple[str, dict | None, list[str]]:
    config = resolve_upstream_config(scope, settings)

    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if config.api_key:
        headers[config.api_key_header] = _normalize_api_key_header_value(
            config.api_key_header, config.api_key
        )

    payload = {
        "message": prompt,
        "stream": False,
    }

    try:
        timeout = httpx.Timeout(float(settings.ai_timeout_sec))
        with httpx.Client(timeout=timeout) as client:
            response = client.post(config.endpoint, headers=headers, json=payload)
    except httpx.TimeoutException as exc:
        raise ApiError(
            status_code=504,
            code="chat_upstream_timeout",
            message=f"{scope.capitalize()} AI upstream timed out.",
        ) from exc
    except httpx.HTTPError as exc:
        raise ApiError(
            status_code=502,
            code="chat_upstream_unreachable",
            message=f"Could not reach {scope} AI upstream endpoint.",
        ) from exc

    if response.status_code >= 400:
        detail = response.text.strip()
        raise ApiError(
            status_code=502,
            code="chat_upstream_error",
            message=(
                f"{scope.capitalize()} AI upstream error ({response.status_code})."
                + (f" {detail}" if detail else "")
            ),
        )

    warnings: list[str] = []
    usage: dict | None = None

    content_type = (response.headers.get("content-type") or "").lower()
    if "application/json" in content_type:
        try:
            parsed = response.json()
        except ValueError as exc:
            raise ApiError(
                status_code=502,
                code="chat_upstream_bad_json",
                message=f"{scope.capitalize()} AI upstream returned invalid JSON.",
            ) from exc

        reply = extract_reply(parsed if isinstance(parsed, dict) else {})
        if not reply:
            raise ApiError(
                status_code=502,
                code="chat_upstream_empty",
                message=f"{scope.capitalize()} AI upstream returned an empty response.",
            )

        if isinstance(parsed, dict) and isinstance(parsed.get("usage"), dict):
            usage = parsed.get("usage")

        return reply, usage, warnings

    # Fallback for text/plain responses.
    text = response.text.strip()
    if not text:
        raise ApiError(
            status_code=502,
            code="chat_upstream_empty",
            message=f"{scope.capitalize()} AI upstream returned an empty response.",
        )

    warnings.append("Upstream returned non-JSON response; parsed as plain text.")
    return text, usage, warnings
