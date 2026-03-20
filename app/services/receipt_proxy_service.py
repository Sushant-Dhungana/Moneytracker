from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
from urllib.parse import urlparse

import httpx

from app.core.config import Settings
from app.core.errors import ApiError

_TEXTRACT_CONTENT_TYPE = "application/x-amz-json-1.1"
_ALLOWED_TEXTRACT_TARGETS = {
    "Textract.AnalyzeExpense",
    "Textract.DetectDocumentText",
}


def _normalize_textract_endpoint(settings: Settings) -> str:
    configured = str(settings.receipt_textract_endpoint or "").strip()
    if configured:
        return configured if configured.endswith("/") else f"{configured}/"
    region = str(settings.receipt_aws_region).strip() or "us-east-1"
    return f"https://textract.{region}.amazonaws.com/"


def _signing_key(secret_access_key: str, date_stamp: str, region: str) -> bytes:
    key_date = hmac.new(
        f"AWS4{secret_access_key}".encode("utf-8"),
        date_stamp.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    key_region = hmac.new(key_date, region.encode("utf-8"), hashlib.sha256).digest()
    key_service = hmac.new(key_region, b"textract", hashlib.sha256).digest()
    return hmac.new(key_service, b"aws4_request", hashlib.sha256).digest()


def _build_signed_headers(
    *,
    endpoint: str,
    target: str,
    request_body: str,
    settings: Settings,
) -> dict[str, str]:
    access_key_id = str(settings.receipt_aws_access_key_id or "").strip()
    secret_access_key = str(settings.receipt_aws_secret_access_key or "").strip()
    if not access_key_id or not secret_access_key:
        raise ApiError(
            status_code=500,
            code="receipt_proxy_not_configured",
            message=(
                "Receipt proxy credentials are not configured on backend. "
                "Set RECEIPT_AWS_ACCESS_KEY_ID and RECEIPT_AWS_SECRET_ACCESS_KEY."
            ),
        )

    parsed_endpoint = urlparse(endpoint)
    host = parsed_endpoint.netloc.strip()
    canonical_uri = parsed_endpoint.path or "/"
    if not host:
        raise ApiError(
            status_code=500,
            code="receipt_proxy_bad_endpoint",
            message="Receipt proxy Textract endpoint is invalid.",
        )

    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    region = str(settings.receipt_aws_region).strip() or "us-east-1"
    payload_hash = hashlib.sha256(request_body.encode("utf-8")).hexdigest()

    canonical_header_map = {
        "content-type": _TEXTRACT_CONTENT_TYPE,
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
        "x-amz-target": target,
    }
    session_token = str(settings.receipt_aws_session_token or "").strip()
    if session_token:
        canonical_header_map["x-amz-security-token"] = session_token

    sorted_header_keys = sorted(canonical_header_map)
    canonical_headers = "".join(
        f"{header_name}:{canonical_header_map[header_name]}\n"
        for header_name in sorted_header_keys
    )
    signed_headers = ";".join(sorted_header_keys)
    canonical_request = "\n".join(
        [
            "POST",
            canonical_uri,
            "",
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )
    credential_scope = f"{date_stamp}/{region}/textract/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )

    signature = hmac.new(
        _signing_key(secret_access_key, date_stamp, region),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    headers = dict(canonical_header_map)
    headers["authorization"] = (
        "AWS4-HMAC-SHA256 "
        f"Credential={access_key_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, "
        f"Signature={signature}"
    )
    return headers


def proxy_receipt_request(
    *,
    target: str,
    payload: dict,
    settings: Settings,
) -> dict:
    normalized_target = str(target).strip()
    if normalized_target not in _ALLOWED_TEXTRACT_TARGETS:
        raise ApiError(
            status_code=400,
            code="invalid_receipt_target",
            message="Unsupported receipt target.",
        )

    request_body = json.dumps(payload)
    endpoint = _normalize_textract_endpoint(settings)
    headers = _build_signed_headers(
        endpoint=endpoint,
        target=normalized_target,
        request_body=request_body,
        settings=settings,
    )

    try:
        timeout = httpx.Timeout(float(settings.receipt_timeout_sec))
        with httpx.Client(timeout=timeout) as client:
            response = client.post(endpoint, headers=headers, content=request_body)
    except httpx.TimeoutException as exc:
        raise ApiError(
            status_code=504,
            code="receipt_upstream_timeout",
            message="Receipt scan upstream timed out.",
        ) from exc
    except httpx.HTTPError as exc:
        raise ApiError(
            status_code=502,
            code="receipt_upstream_unreachable",
            message="Could not reach receipt scan upstream.",
        ) from exc

    if response.status_code >= 400:
        detail = response.text.strip()
        raise ApiError(
            status_code=502,
            code="receipt_upstream_error",
            message=(
                f"Receipt scan upstream error ({response.status_code})."
                + (f" {detail}" if detail else "")
            ),
        )

    try:
        parsed = response.json()
    except ValueError as exc:
        raise ApiError(
            status_code=502,
            code="receipt_upstream_bad_json",
            message="Receipt scan upstream returned invalid JSON.",
        ) from exc

    if not isinstance(parsed, dict):
        raise ApiError(
            status_code=502,
            code="receipt_upstream_bad_json",
            message="Receipt scan upstream returned an unsupported JSON payload.",
        )

    return parsed
