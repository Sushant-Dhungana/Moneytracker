from datetime import datetime
from decimal import Decimal
from uuid import UUID

from typing import Literal

from pydantic import BaseModel, Field


class BusinessRpcRequest(BaseModel):
    name: str
    params: dict


class BusinessRpcResponse(BaseModel):
    data: dict | list | str | int | float | bool | None = None


class BusinessAccountItem(BaseModel):
    id: UUID
    user_id: UUID
    profile_id: UUID
    name: str
    type: str
    institution_name: str | None = None
    account_number: str | None = None
    qr_image_url: str | None = None
    opening_balance: Decimal | None = None
    current_balance: Decimal | None = None
    overdraft_limit: Decimal | None = None
    is_active: bool
    created_at: datetime | None = None


class BusinessAccountsResponse(BaseModel):
    items: list[BusinessAccountItem]


class BusinessAccountNameItem(BaseModel):
    id: UUID
    name: str
    type: str


class BusinessAccountNamesResponse(BaseModel):
    items: list[BusinessAccountNameItem]


class BusinessAccountUpdateRequest(BaseModel):
    profile_id: str
    name: str
    institution_name: str
    account_number: str | None = None
    qr_image_url: str | None = None


class BusinessAccountTransferRequest(BaseModel):
    profile_id: str
    from_account_id: str
    to_account_id: str
    amount: float = Field(gt=0)
    date: str
    description: str | None = None
    metadata: dict | None = None


class BusinessAccountTransferResponse(BaseModel):
    entry_id: UUID


class BusinessStockInBatchItem(BaseModel):
    product_id: str
    category_id: str
    sku: str | None = None
    qty: float = Field(gt=0)
    unit_cost: float = Field(gt=0)
    selling_price: float = Field(ge=0)
    entry_source: str = "stock_in"
    paid_amount: float | None = None


class BusinessStockInBatchRequest(BaseModel):
    profile_id: str
    supplier_id: str | None = None
    payment_mode: str
    account_id: str | None = None
    date: str
    note: str | None = None
    idempotency_key: str | None = None
    items: list[BusinessStockInBatchItem] = Field(min_length=1)


class BusinessStockInBatchResponse(BaseModel):
    entry_ids: list[str]
    product_ids: list[str]
    total_amount: float
    total_paid_amount: float
    supplier_id: str | None = None
    occurred_on: str
    expense_delta: float = 0
    payable_delta: float = 0
    account_delta: float = 0
    inventory_deltas: list[dict[str, float | str]] = Field(default_factory=list)


class BusinessSaleItem(BaseModel):
    product_id: str
    qty: float = Field(gt=0)
    rate: float = Field(ge=0)


class BusinessSaleRequest(BaseModel):
    user_id: str | None = None
    profile_id: str
    customer_id: str
    date: str
    items: list[BusinessSaleItem] = Field(min_length=1)
    discount: float = 0
    tax: float = 0
    payment_mode: str
    paid_amount: float = 0
    account_id: str | None = None
    note: str | None = None
    idempotency_key: str | None = None


class BusinessSaleResponse(BaseModel):
    invoice_id: str
    entry_id: str | None = None
    product_ids: list[str]
    total_amount: float
    paid_amount: float
    due_amount: float
    occurred_on: str
    account_delta: float = 0


class BusinessProductCreateRequest(BaseModel):
    profile_id: str
    name: str
    price: float = 0
    selling_price: float = 0
    quantity: float = 0
    unit_id: str
    category_id: str | None = None
    sku: str | None = None
    opening_qty: float = Field(default=0, ge=0)
    opening_unit_cost: float = Field(default=0, ge=0)
    opening_date: str | None = None
    opening_note: str | None = None
    idempotency_key: str | None = None


class BusinessProductListItem(BaseModel):
    id: str
    user_id: str
    profile_id: str
    name: str
    price: float = 0
    selling_price: float = 0
    quantity: float = 0
    unit_id: str
    category_id: str | None = None
    sku: str | None = None
    is_active: bool = True
    created_at: str | None = None
    updated_at: str | None = None
    unit_name: str | None = None
    category_name: str | None = None


class BusinessProductCreateResponse(BaseModel):
    product: BusinessProductListItem
    opening_posted: bool = False
    occurred_on: str


class BusinessTransactionProductAddedDetails(BaseModel):
    sku: str | None = None
    quantity: float = 0
    unit_cost: float = 0


class BusinessTransactionFeedItem(BaseModel):
    id: str
    entry_id: str | None = None
    txn_type: str
    amount: float
    signed_amount: float
    date: str
    created_at: str | None = None
    title: str
    context_text: str | None = None
    description: str | None = None
    account_tag_label: str
    is_due: bool = False
    section: Literal["posting", "customer", "supplier"]
    expandable: bool = False
    product_added_details: BusinessTransactionProductAddedDetails | None = None


class BusinessTransactionsFeedResponse(BaseModel):
    items: list[BusinessTransactionFeedItem]
    next_cursor: str | None = None
    has_more: bool = False
    posting_count: int = 0
    customer_count: int = 0
    supplier_count: int = 0
    period: str = "all"
    from_date: str | None = None
    to_date: str | None = None


class BusinessPosBootstrapProductUnit(BaseModel):
    id: str
    name: str


class BusinessPosBootstrapProductCategoryRef(BaseModel):
    id: str
    name: str


class BusinessPosBootstrapProduct(BaseModel):
    id: str
    user_id: str
    profile_id: str
    name: str
    price: float = 0
    selling_price: float = 0
    quantity: float = 0
    unit_id: str | None = None
    category_id: str | None = None
    sku: str | None = None
    is_active: bool = True
    created_at: str
    updated_at: str
    unit: BusinessPosBootstrapProductUnit | None = None
    category: BusinessPosBootstrapProductCategoryRef | None = None


class BusinessPosBootstrapCategory(BaseModel):
    id: str
    user_id: str
    profile_id: str
    domain: str
    name: str
    parent_id: str | None = None
    is_active: bool = True
    created_at: str
    updated_at: str


class BusinessPosBootstrapResponse(BaseModel):
    products: list[BusinessPosBootstrapProduct]
    product_categories: list[BusinessPosBootstrapCategory]
    payment_accounts: list[BusinessAccountItem]


class BusinessCustomerSearchItem(BaseModel):
    id: str
    user_id: str
    profile_id: str
    name: str
    phone: str | None = None
    address: str | None = None
    category_id: str | None = None
    credit_limit: float = 0
    opening_balance: float | None = None
    opening_balance_type: str | None = None
    reminder_date: str | None = None
    is_active: bool = True
    created_at: str
    updated_at: str


class BusinessCustomerSearchResponse(BaseModel):
    items: list[BusinessCustomerSearchItem]


class BusinessCustomerHistoryPurchaseItem(BaseModel):
    id: str
    invoice_no: str
    amount: float = 0
    due_amount: float = 0
    date: str
    payment_status: Literal["paid", "partial", "due"]
    created_at: str | None = None


class BusinessCustomerHistoryPaymentItem(BaseModel):
    id: str
    amount: float = 0
    date: str
    mode: Literal["cash", "bank", "merchant", "credit", "partial"] | None = None
    source: Literal["invoice_payment", "receivable_collection"]
    created_at: str | None = None


class BusinessCustomerHistoryResponse(BaseModel):
    purchases: list[BusinessCustomerHistoryPurchaseItem]
    payments: list[BusinessCustomerHistoryPaymentItem]


class BusinessProductSalesSummaryResponse(BaseModel):
    product_id: str
    product_name: str
    period: str
    from_date: str | None = None
    to_date: str | None = None
    qty_sold: float = 0
    sales_amount: float = 0
    invoice_count: int = 0
