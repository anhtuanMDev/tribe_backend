# views.py
from api.constants import VerificationPurpose
import random
from api.serializers import (
    RequestVerificationSerializer,
    ConfirmVerificationSerializer,
    ResetPasswordSerializer,
    RegisterSerializer,
    LoginSerializer,
    VerifyEmailSerializer,
    ChangePasswordSerializer,  # new — add to serializers.py below
)
from api.models import VerificationRequest
from django.http import JsonResponse
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework_simplejwt.tokens import RefreshToken
from django.contrib.auth import get_user_model
from django.template.loader import render_to_string
from django.core.mail import EmailMultiAlternatives
from django.conf import settings
from datetime import datetime
from django_ratelimit.decorators import ratelimit
from django.utils import timezone

User = get_user_model()


# ─────────────────────────────────────────────
# SHARED UTILITY
# ─────────────────────────────────────────────

SUBJECT_MAP = {
    VerificationPurpose.REGISTER: "Verify your Tribe account",
    VerificationPurpose.RESET_PASSWORD: "Reset your Tribe password",
    VerificationPurpose.DELETE_ACCOUNT: "Confirm account deletion",
    VerificationPurpose.CHANGE_PASSWORD: "Confirm password change",
}


def _send_verification(user, purpose):
    """
    Invalidate all pending codes for this purpose, create a fresh one, send email.
    Single source of truth — used by every flow.
    """
    VerificationRequest.objects.filter(
        user=user,
        purpose=purpose,
        is_verified=False,
        invalidated_at__isnull=True,
    ).update(invalidated_at=timezone.now())

    verification = VerificationRequest.objects.create(
        user=user,
        purpose=purpose,
        code=str(random.randint(100000, 999999)),
    )

    html_content = render_to_string(
        "emails/verify_email.html",
        {
            "verification_code": verification.code,
            "expiration_minutes": 10,
            "year": datetime.now().year,
        },
    )
    msg = EmailMultiAlternatives(
        subject=SUBJECT_MAP.get(purpose, "Verify your identity"),
        body=f"Your verification code is: {verification.code}",
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[user.email],
    )
    msg.attach_alternative(html_content, "text/html")
    msg.send()
    return verification


def _get_verified_request(user, purpose):
    """
    Fetch the latest verified+non-invalidated request for a purpose.
    Used by action views (reset password, delete account, change password)
    that execute AFTER ConfirmVerificationView marks is_verified=True.
    Raises VerificationRequest.DoesNotExist if none found.
    """
    return VerificationRequest.objects.filter(
        user=user,
        purpose=purpose,
        is_verified=True,
        invalidated_at__isnull=True,
    ).latest("created_at")


# ─────────────────────────────────────────────
# HEALTH
# ─────────────────────────────────────────────


@ratelimit(key="ip", rate="1/4m", block=True)
def health_check(request):
    return JsonResponse({"status": "ok"})


# ─────────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────────


class LoginView(APIView):
    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"error": "Email or password is incorrect."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        email = serializer.validated_data["email"]
        password = serializer.validated_data["password"]

        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            return Response(
                {"error": "Email or password is incorrect."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not user.check_password(password):
            return Response(
                {"error": "Email or password is incorrect."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if user.is_deleted:
            if user.is_restorable:
                user.restore()
            else:
                return Response(
                    {"error": "This account no longer exists."},
                    status=status.HTTP_401_UNAUTHORIZED,
                )

        if not user.is_active:
            return Response(
                {"error": "Please verify your email first."},
                status=status.HTTP_409_CONFLICT,
            )

        refresh = RefreshToken.for_user(user)
        return Response({"token": str(refresh.access_token), "refresh": str(refresh)})


class RegisterView(APIView):
    def post(self, request):
        email = request.data.get("email")

        if email:
            try:
                existing = User.objects.get(email=email)

                if existing.is_deleted and existing.is_restorable:
                    return Response(
                        {
                            "error": "This account was recently deleted. Log in to restore it."
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                if existing.is_deleted and not existing.is_restorable:
                    # Free up the email slot, fall through to create fresh user
                    existing.permanent_delete()

                elif not existing.is_active:
                    # Unverified ghost account — resend code, don't create a new user
                    _send_verification(existing, VerificationPurpose.REGISTER)
                    return Response(
                        {
                            "message": "Registration successful. Check your email for the verification code."
                        },
                        status=status.HTTP_201_CREATED,
                    )

                else:
                    return Response(
                        {"error": "An account with this email already exists."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

            except User.DoesNotExist:
                pass

        serializer = RegisterSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        user = serializer.save()
        user.is_active = False
        user.save()
        _send_verification(user, VerificationPurpose.REGISTER)

        return Response(
            {
                "message": "Registration successful. Check your email for the verification code."
            },
            status=status.HTTP_201_CREATED,
        )


class VerifyEmailView(APIView):
    """Step 2 of registration — activates the account and returns tokens."""

    def post(self, request):
        serializer = VerifyEmailSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        email = serializer.validated_data["email"]
        code = serializer.validated_data["code"]

        try:
            user = User.objects.get(email=email)

            if user.is_active:
                return Response(
                    {"error": "This account is already verified."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            verification = VerificationRequest.objects.filter(
                user=user,
                purpose=VerificationPurpose.REGISTER,
                is_verified=False,
                invalidated_at__isnull=True,
            ).latest("created_at")

            if verification.is_expired:
                verification.invalidate()
                return Response(
                    {"error": "Code expired. Please register again to get a new code."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if verification.code != code:
                return Response(
                    {"error": "Invalid code."}, status=status.HTTP_400_BAD_REQUEST
                )

            user.is_active = True
            user.save()
            verification.is_verified = True
            verification.save()

            refresh = RefreshToken.for_user(user)
            return Response(
                {"token": str(refresh.access_token), "refresh": str(refresh)}
            )

        except (User.DoesNotExist, VerificationRequest.DoesNotExist):
            return Response(
                {"error": "Invalid request."}, status=status.HTTP_400_BAD_REQUEST
            )


# ─────────────────────────────────────────────
# VERIFICATION (shared request + confirm)
# ─────────────────────────────────────────────


class RequestVerificationView(APIView):
    """
    Request a verification code for: reset_password, delete_account, change_password.
    Always returns the same message to prevent email enumeration.
    """

    def post(self, request):
        serializer = RequestVerificationSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        email = serializer.validated_data["email"]
        purpose = serializer.validated_data["purpose"]

        try:
            user = User.objects.get(email=email, is_active=True)
            _send_verification(user, purpose)
        except User.DoesNotExist:
            pass  # silent — don't leak existence

        return Response(
            {"message": "If that email exists you will receive a verification code."}
        )


class ConfirmVerificationView(APIView):
    """
    Validates the code and marks it verified.
    Must be called BEFORE the action view (reset password, delete account, change password).
    """

    def post(self, request):
        serializer = ConfirmVerificationSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        email = serializer.validated_data["email"]
        code = serializer.validated_data["code"]
        purpose = serializer.validated_data["purpose"]

        try:
            user = User.objects.get(email=email)
            verification = VerificationRequest.objects.filter(
                user=user,
                purpose=purpose,
                is_verified=False,
                invalidated_at__isnull=True,
            ).latest("created_at")

            if verification.is_expired:
                verification.invalidate()
                return Response(
                    {"error": "Code expired."}, status=status.HTTP_400_BAD_REQUEST
                )

            if verification.code != code:
                return Response(
                    {"error": "Invalid code."}, status=status.HTTP_400_BAD_REQUEST
                )

            verification.is_verified = True
            verification.save()

            return Response({"message": "Verified successfully."})

        except (User.DoesNotExist, VerificationRequest.DoesNotExist):
            return Response(
                {"error": "Invalid request."}, status=status.HTTP_400_BAD_REQUEST
            )


# ─────────────────────────────────────────────
# ACTIONS (all require a prior ConfirmVerification)
# ─────────────────────────────────────────────


class ResetPasswordView(APIView):
    """
    Unauthenticated — for forgot password flow.
    Requires a verified RESET_PASSWORD request.
    """

    def post(self, request):
        serializer = ResetPasswordSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        email = serializer.validated_data["email"]
        new_password = serializer.validated_data["new_password"]

        try:
            user = User.objects.get(email=email)
            verification = _get_verified_request(
                user, VerificationPurpose.RESET_PASSWORD
            )

            if verification.is_expired:
                verification.invalidate()
                return Response(
                    {"error": "Verification expired. Please request a new code."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            user.set_password(new_password)
            user.save()
            verification.invalidate()

            return Response({"message": "Password reset successful."})

        except (User.DoesNotExist, VerificationRequest.DoesNotExist):
            return Response(
                {"error": "Invalid request. Please verify your email first."},
                status=status.HTTP_400_BAD_REQUEST,
            )


class ChangePasswordView(APIView):
    """
    Authenticated — user must be logged in.
    Flow: request code (change_password) → confirm → POST here with current + new password.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = ChangePasswordSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        current_password = serializer.validated_data["current_password"]
        new_password = serializer.validated_data["new_password"]
        user = request.user

        if not user.check_password(current_password):
            return Response(
                {"error": "Current password is incorrect."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            verification = _get_verified_request(
                user, VerificationPurpose.CHANGE_PASSWORD
            )

            if verification.is_expired:
                verification.invalidate()
                return Response(
                    {"error": "Verification expired. Please request a new code."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            user.set_password(new_password)
            user.save()
            verification.invalidate()

            return Response({"message": "Password changed successfully."})

        except VerificationRequest.DoesNotExist:
            return Response(
                {"error": "Please verify your identity first."},
                status=status.HTTP_400_BAD_REQUEST,
            )


class DeleteAccountView(APIView):
    """
    Authenticated — user must be logged in.
    Flow: request code (delete_account) → confirm → POST here.
    Soft deletes — restorable within 1 week via login.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user

        try:
            verification = _get_verified_request(
                user, VerificationPurpose.DELETE_ACCOUNT
            )

            if verification.is_expired:
                verification.invalidate()
                return Response(
                    {"error": "Verification expired. Please request a new code."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            verification.invalidate()
            user.soft_delete()

            return Response(
                {
                    "message": "Account deleted. You have 1 week to restore it by logging in."
                }
            )

        except VerificationRequest.DoesNotExist:
            return Response(
                {"error": "Please verify your identity first."},
                status=status.HTTP_400_BAD_REQUEST,
            )
