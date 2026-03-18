from pydantic import BaseModel


class PersonalSummaryResponse(BaseModel):
    profile_id: str
    period: str = "all"
    from_date: str | None = None
    to_date: str | None = None
    income: float = 0
    expense: float = 0
    net: float = 0
    transaction_count: int = 0


class BusinessSummaryResponse(BaseModel):
    profile_id: str
    period: str = "all"
    from_date: str | None = None
    to_date: str | None = None
    sales: float = 0
    income: float = 0
    expense: float = 0
    net: float = 0
    receivable_due: float = 0
    payable_due: float = 0


class BusinessDueSummaryResponse(BaseModel):
    profile_id: str
    receivable_due_total: float = 0
    payable_due_total: float = 0
    outstanding_invoice_count: int = 0
    outstanding_invoice_due_total: float = 0
    customer_due_count: int = 0
    supplier_due_count: int = 0
    customer_due_breakdown: list[dict] = []
    supplier_due_breakdown: list[dict] = []
