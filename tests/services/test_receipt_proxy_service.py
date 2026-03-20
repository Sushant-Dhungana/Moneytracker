from types import SimpleNamespace

import pytest

from app.core.errors import ApiError
from app.services.receipt_proxy_service import proxy_receipt_request


def _settings(**overrides):
    base = {
        "receipt_aws_access_key_id": "AKIATESTKEY",
        "receipt_aws_secret_access_key": "test-secret-key",
        "receipt_aws_session_token": None,
        "receipt_aws_region": "us-east-1",
        "receipt_textract_endpoint": None,
        "receipt_timeout_sec": 30,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_proxy_receipt_request_signs_and_forwards(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    class _Response:
        status_code = 200

        def json(self):
            return {"ExpenseDocuments": []}

    class _Client:
        def __init__(self, *, timeout):
            captured["timeout"] = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, url, *, headers, content):
            captured["url"] = url
            captured["headers"] = headers
            captured["content"] = content
            return _Response()

    monkeypatch.setattr("app.services.receipt_proxy_service.httpx.Client", _Client)

    response = proxy_receipt_request(
        target="Textract.AnalyzeExpense",
        payload={"Document": {"Bytes": "abc123"}},
        settings=_settings(),
    )

    assert response == {"ExpenseDocuments": []}
    assert captured["url"] == "https://textract.us-east-1.amazonaws.com/"
    assert captured["headers"]["x-amz-target"] == "Textract.AnalyzeExpense"
    assert captured["headers"]["content-type"] == "application/x-amz-json-1.1"
    assert "authorization" in captured["headers"]


def test_proxy_receipt_request_requires_credentials() -> None:
    with pytest.raises(ApiError) as exc_info:
        proxy_receipt_request(
            target="Textract.AnalyzeExpense",
            payload={"Document": {"Bytes": "abc123"}},
            settings=_settings(receipt_aws_access_key_id=None, receipt_aws_secret_access_key=None),
        )

    assert exc_info.value.code == "receipt_proxy_not_configured"
