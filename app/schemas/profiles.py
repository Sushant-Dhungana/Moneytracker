from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class ProfileRow(BaseModel):
    id: UUID | str
    user_id: UUID | str
    profile_type: str
    name: str | None = None
    phone_number: str | None = None
    address: str | None = None
    pan_number: str | None = None
    created_at: datetime | str
    updated_at: datetime | str


class ActiveProfileStateResponse(BaseModel):
    profiles: list[ProfileRow]
    activeProfileId: UUID | str | None = None
    activeProfileType: str = "personal"
    hasBusinessProfile: bool = False
    hasBusinessSetupComplete: bool = False


class SwitchProfileRequest(BaseModel):
    target_type: str


class SwitchProfileResponse(BaseModel):
    status: str
    profile: ProfileRow | None = None


class UpsertBusinessProfileRequest(BaseModel):
    name: str
    phone_number: str | None = None
    address: str | None = None
    pan_number: str | None = None
    opening_balance: float | None = None
