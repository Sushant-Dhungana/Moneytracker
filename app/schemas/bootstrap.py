from pydantic import BaseModel

from app.schemas.profiles import ProfileRow


class MobileBootstrapResponse(BaseModel):
    profiles: list[ProfileRow]
    activeProfileId: str | None = None
    activeProfileType: str = "personal"
    hasBusinessProfile: bool = False
    hasBusinessSetupComplete: bool = False
    isPersonalProfileComplete: bool = False
    hasCashAccount: bool = False
    currencyCode: str = "NPR"
    lightweightSummary: dict = {}
