from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class ReceiptProxyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: Literal["Textract.AnalyzeExpense", "Textract.DetectDocumentText"]
    payload: dict[str, Any]
