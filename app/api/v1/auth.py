from fastapi import APIRouter, Depends

from app.core.config import Settings, get_settings
from app.core.errors import ApiError
from app.core.supabase_auth_client import supabase_auth_post
from app.schemas.auth import (
    AuthOperationResponse,
    AuthUserResponse,
    ForgotPasswordRequest,
    ResendSignupOtpRequest,
    ResetPasswordWithOtpRequest,
    SendOtpRequest,
    SignInRequest,
    SignUpRequest,
    VerifyOtpRequest,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _map_user(user: dict | None) -> AuthUserResponse | None:
    if not user:
        return None
    metadata = user.get("user_metadata") or {}
    username = (
        metadata.get("username")
        or metadata.get("full_name")
        or metadata.get("name")
        or user.get("email", "").split("@")[0]
    )
    return AuthUserResponse(
        id=str(user.get("id", "")),
        email=user.get("email"),
        username=username,
        created_at=user.get("created_at"),
        updated_at=user.get("updated_at") or user.get("created_at"),
    )


@router.post("/signup", response_model=AuthOperationResponse)
def sign_up(payload: SignUpRequest, settings: Settings = Depends(get_settings)) -> AuthOperationResponse:
    data = supabase_auth_post(
        settings,
        "/signup",
        {
            "email": payload.email,
            "password": payload.password,
            "data": payload.metadata or {},
        },
    )
    user = _map_user(data.get("user"))
    session = data.get("session") or {}
    has_signup_session = bool(session.get("access_token"))
    needs_confirmation = bool(user and not has_signup_session)
    if settings.auth_require_email_confirmation and has_signup_session:
        raise ApiError(
            status_code=409,
            code="signup_verification_not_enabled",
            message=(
                "Signup returned an immediate session token, so OTP email confirmation is not active "
                "for this project. In Supabase Auth settings, enable 'Confirm email' (not just "
                "'Allow new users to sign up') and save changes."
            ),
        )
    return AuthOperationResponse(
        user=user if not needs_confirmation else None,
        needsEmailConfirmation=needs_confirmation,
        access_token=session.get("access_token"),
        refresh_token=session.get("refresh_token"),
    )


@router.post("/signin", response_model=AuthOperationResponse)
def sign_in(payload: SignInRequest, settings: Settings = Depends(get_settings)) -> AuthOperationResponse:
    data = supabase_auth_post(
        settings,
        "/token?grant_type=password",
        {"email": payload.email, "password": payload.password},
    )
    user = _map_user(data.get("user"))
    return AuthOperationResponse(
        user=user,
        access_token=data.get("access_token"),
        refresh_token=data.get("refresh_token"),
    )


@router.post("/send-otp", response_model=AuthOperationResponse)
def send_otp(payload: SendOtpRequest, settings: Settings = Depends(get_settings)) -> AuthOperationResponse:
    supabase_auth_post(
        settings,
        "/otp",
        {
            "email": payload.email,
            "create_user": payload.should_create_user,
        },
    )
    return AuthOperationResponse()


@router.post("/resend-signup-otp", response_model=AuthOperationResponse)
def resend_signup_otp(
    payload: ResendSignupOtpRequest, settings: Settings = Depends(get_settings)
) -> AuthOperationResponse:
    supabase_auth_post(
        settings,
        "/resend",
        {"type": "signup", "email": payload.email},
    )
    return AuthOperationResponse()


@router.post("/verify-otp-login", response_model=AuthOperationResponse)
def verify_otp_login(
    payload: VerifyOtpRequest, settings: Settings = Depends(get_settings)
) -> AuthOperationResponse:
    otp_type = (payload.otp_type or "email").strip().lower()
    if otp_type not in {"email", "signup"}:
        raise ApiError(
            status_code=400,
            code="invalid_otp_type",
            message="Invalid OTP type. Use email or signup.",
        )

    # Supabase OTP/email flows vary by deployment and template mode.
    # Try compatible verification modes/endpoints before failing.
    verify_data = None
    type_candidates = [otp_type]
    if otp_type == "email":
        type_candidates.extend(["magiclink", "signup"])
    else:
        type_candidates.extend(["email", "magiclink"])

    last_error: ApiError | None = None
    for candidate in type_candidates:
        for verify_path in ("/verify", "/token?grant_type=otp"):
            try:
                verify_data = supabase_auth_post(
                    settings,
                    verify_path,
                    {"type": candidate, "email": payload.email, "token": payload.otp},
                )
                last_error = None
                break
            except ApiError as verify_error:
                last_error = verify_error

        if verify_data is not None:
            break

    if last_error:
        raise last_error

    access_token = (verify_data or {}).get("access_token")
    refresh_token = (verify_data or {}).get("refresh_token")

    if payload.password and access_token:
        supabase_auth_post(
            settings,
            "/user",
            {"password": payload.password},
            bearer=access_token,
        )

    user = _map_user((verify_data or {}).get("user"))
    return AuthOperationResponse(
        user=user,
        access_token=access_token,
        refresh_token=refresh_token,
    )


@router.post("/forgot-password", response_model=AuthOperationResponse)
def forgot_password(
    payload: ForgotPasswordRequest, settings: Settings = Depends(get_settings)
) -> AuthOperationResponse:
    supabase_auth_post(settings, "/recover", {"email": payload.email})
    return AuthOperationResponse()


@router.post("/reset-password-otp", response_model=AuthOperationResponse)
def reset_password_with_otp(
    payload: ResetPasswordWithOtpRequest, settings: Settings = Depends(get_settings)
) -> AuthOperationResponse:
    verify_data = None
    last_error = None
    for otp_type in ("email", "recovery"):
        try:
            verify_data = supabase_auth_post(
                settings,
                "/verify",
                {"type": otp_type, "email": payload.email, "token": payload.otp},
            )
            last_error = None
            break
        except Exception as exc:
            last_error = exc

    if last_error:
        raise last_error

    access_token = (verify_data or {}).get("access_token")
    refresh_token = (verify_data or {}).get("refresh_token")
    if not access_token:
        return AuthOperationResponse(error="Invalid reset session.", errorType="business")

    supabase_auth_post(
        settings,
        "/user",
        {"password": payload.new_password},
        bearer=access_token,
    )

    user = _map_user((verify_data or {}).get("user"))
    return AuthOperationResponse(
        user=user,
        access_token=access_token,
        refresh_token=refresh_token,
    )
