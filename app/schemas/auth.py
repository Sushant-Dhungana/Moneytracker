from pydantic import BaseModel, Field


class SignUpRequest(BaseModel):
    email: str
    password: str
    metadata: dict | None = None


class SignInRequest(BaseModel):
    email: str
    password: str


class SendOtpRequest(BaseModel):
    email: str
    should_create_user: bool = True


class VerifyOtpRequest(BaseModel):
    email: str
    otp: str
    password: str | None = None
    otp_type: str | None = None


class ResendSignupOtpRequest(BaseModel):
    email: str


class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordWithOtpRequest(BaseModel):
    email: str
    otp: str
    new_password: str = Field(min_length=6)


class AuthUserResponse(BaseModel):
    id: str
    email: str | None = None
    username: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class AuthOperationResponse(BaseModel):
    error: str | None = None
    errorType: str | None = None
    user: AuthUserResponse | None = None
    needsEmailConfirmation: bool | None = None
    access_token: str | None = None
    refresh_token: str | None = None
