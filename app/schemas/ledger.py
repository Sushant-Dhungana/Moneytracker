from pydantic import BaseModel, Field


class IncomeExpenseCreateRequest(BaseModel):
    type: str = Field(pattern="^(income|expense)$")
    amount: float = Field(gt=0)
    account_id: str
    category_id: str | None = None
    description: str | None = None
    date: str
    attachment_url: str | None = None
    transaction_id: str | None = None


class IncomeExpenseCreateResponse(BaseModel):
    id: str
    entry_id: str | None = None
    user_id: str
    account_id: str
    category_id: str | None = None
    type: str
    amount: float
    description: str | None = None
    date: str
    created_at: str
    updated_at: str


class IncomeExpenseUpdateRequest(BaseModel):
    amount: float = Field(gt=0)
    account_id: str
    category_id: str | None = None
    description: str | None = None
    date: str
    attachment_url: str | None = None


class IncomeExpenseDeleteRequest(BaseModel):
    reason: str | None = "User deleted transaction"


class LedgerEntryIdResponse(BaseModel):
    entry_id: str


class TransferCreateRequest(BaseModel):
    from_account_id: str
    to_account_id: str
    amount: float = Field(gt=0)
    date: str
    description: str | None = None
    attachment_url: str | None = None
    metadata: dict | None = None


class LoanOutCreateRequest(BaseModel):
    counterparty_id: str
    from_account_id: str
    amount: float = Field(gt=0)
    date: str
    description: str | None = None
    attachment_url: str | None = None
    metadata: dict | None = None


class LoanInCreateRequest(BaseModel):
    counterparty_id: str
    to_account_id: str
    amount: float = Field(gt=0)
    date: str
    description: str | None = None
    attachment_url: str | None = None
    metadata: dict | None = None


class RepaymentInCreateRequest(BaseModel):
    counterparty_id: str
    to_account_id: str
    amount: float = Field(gt=0)
    date: str
    description: str | None = None
    attachment_url: str | None = None
    metadata: dict | None = None


class RepaymentOutCreateRequest(BaseModel):
    counterparty_id: str
    from_account_id: str
    amount: float = Field(gt=0)
    date: str
    description: str | None = None
    attachment_url: str | None = None
    metadata: dict | None = None
