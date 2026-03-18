from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict


class ApiResponse(BaseModel):
    ok: bool = True


class PaginationMeta(BaseModel):
    total: int
    limit: int
    offset: int


class HealthResponse(BaseModel):
    status: str
    service: str
    env: str


class ProfileSummary(BaseModel):
    user_id: str
    email: str | None = None
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    country_code: str | None = None
    currency_code: str | None = None
    profile_completed: bool | None = None
    avatar_url: str | None = None
    active_profile_id: str | None = None
    active_profile_type: str | None = None


class ProfileNameUpdateRequest(BaseModel):
    first_name: str
    last_name: str


class ProfileNameUpdateResponse(BaseModel):
    full_name: str
    first_name: str
    last_name: str


class ProfileCompleteRequest(BaseModel):
    first_name: str
    last_name: str
    country_code: str
    currency_code: str


class ProfileCompleteResponse(BaseModel):
    full_name: str
    first_name: str
    last_name: str
    country_code: str
    currency_code: str
    profile_completed: bool


class TransactionFeedItem(BaseModel):
    model_config = ConfigDict(extra="allow")


class TransactionFeedResponse(BaseModel):
    items: list[dict[str, Any]]
    pagination: PaginationMeta


class AccountBalanceItem(BaseModel):
    account_id: str
    account_name: str
    account_type: str
    opening_balance: Decimal
    current_balance: Decimal


class AccountBalancesResponse(BaseModel):
    items: list[AccountBalanceItem]


class TransactionFeedQuery(BaseModel):
    limit: int = 20
    offset: int = 0
    start_date: date | None = None
    end_date: date | None = None
    txn_type: str | None = None
    account_id: str | None = None
    category_id: str | None = None
    counterparty_id: str | None = None
    profile_id: str | None = None


class ServerTimestamp(BaseModel):
    now: datetime
