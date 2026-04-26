from api.constants import VerificationPurpose
from api.models import EmailVerificationCode
import random
from api.serializers import RequestVerificationSerializer
from api.serializers import ConfirmVerificationSerializer
from api.models import VerificationRequest
from django.http import JsonResponse
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework_simplejwt.tokens import RefreshToken
from django.contrib.auth import get_user_model
from django.template.loader import render_to_string
from django.core.mail import EmailMultiAlternatives
from django.conf import settings
from .serializers import (
    ResetPasswordSerializer,
    RegisterSerializer,
    LoginSerializer,
    VerifyEmailSerializer,
)
from datetime import datetime
from django_ratelimit.decorators import ratelimit
from django.utils import timezone
from datetime import timedelta

User = get_user_model()


@ratelimit(key="ip", rate="1/4m", block=True)
def health_check(request):
    return JsonResponse({"status": "ok"})


class LoginView(APIView):
    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        if serializer.is_valid():
            email = serializer.validated_data["email"]
            password = serializer.validated_data["password"]
            try:
                user = User.objects.get(email=email)
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
                            {"error": "Your email hasn’t been verified yet."},
                            status=status.HTTP_401_UNAUTHORIZED,
                        )
                if not user.is_active:
                    return Response(
                        {"error": "Please verify your email first."},
                        status=status.HTTP_409_CONFLICT,
                    )
                refresh = RefreshToken.for_user(user)
                return Response(
                    {
                        "token": str(refresh.access_token),
                        "refresh": str(refresh),
                    }
                )
            except User.DoesNotExist:
                return Response(
                    {"error": "Email or password is incorrect."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        return Response(
            {"error": "Email or password is incorrect."},
            status=status.HTTP_400_BAD_REQUEST,
        )


class RegisterView(APIView):
    def post(self, request):
        email = request.data.get("email")
        if email:
            try:
                existing = User.objects.get(email=email)
                if existing.is_deleted and existing.is_restorable:
                    return Response(
                        {
                            "error": "This account was recently deleted and can still be restored by logging in again."
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                if existing.is_deleted and not existing.is_restorable:
                    # Soft-free the email: mark it as permanently deleted, don't mutate email
                    existing.permanent_delete()  # already exists on your model
                    # Now fall through to create a fresh user
                elif not existing.is_active:
                    # Unverified lingering account — invalidate old codes, reuse account
                    VerificationRequest.objects.filter(
                        user=existing,
                        purpose=VerificationPurpose.REGISTER,
                        is_verified=False,
                        invalidated_at__isnull=True,
                    ).update(invalidated_at=timezone.now())
                    # Re-issue a new code for the existing unverified user
                    _send_register_verification(existing)
                    return Response(
                        {
                            "message": "Registration successful. Check your email for the verification code."
                        },
                        status=status.HTTP_201_CREATED,
                    )
                else:
                    # Active verified account — block
                    return Response(
                        {"error": "An account with this email already exists."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            except User.DoesNotExist:
                pass

        serializer = RegisterSerializer(data=request.data)
        if serializer.is_valid():
            user = serializer.save()
            user.is_active = False
            user.save()
            _send_register_verification(user)
            return Response(
                {
                    "message": "Registration successful. Check your email for the verification code."
                },
                status=status.HTTP_201_CREATED,
            )
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


def _send_register_verification(user):
    """Create a fresh register VerificationRequest and send the email."""
    verification = VerificationRequest.objects.create(
        user=user,
        purpose=VerificationPurpose.REGISTER,
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
        subject="Verify your Tribe account",
        body=f"Your verification code is: {verification.code}",
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[user.email],
    )
    msg.attach_alternative(html_content, "text/html")
    msg.send()
    return verification


class VerifyEmailView(APIView):
    def post(self, request):
        serializer = VerifyEmailSerializer(data=request.data)
        if serializer.is_valid():
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
                    # Don't delete — just soft-invalidate
                    verification.invalidate()
                    return Response(
                        {
                            "error": "Code expired. Please register again to get a new code."
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                if verification.code != code:
                    return Response(
                        {"error": "Invalid code."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                user.is_active = True
                user.save()

                verification.is_verified = True
                verification.save()

                refresh = RefreshToken.for_user(user)
                return Response(
                    {
                        "token": str(refresh.access_token),
                        "refresh": str(refresh),
                    }
                )

            except (User.DoesNotExist, VerificationRequest.DoesNotExist):
                return Response(
                    {"error": "Invalid request."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class ConfirmVerificationView(APIView):
    def post(self, request):
        serializer = ConfirmVerificationSerializer(data=request.data)
        if serializer.is_valid():
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
                ).latest(
                    "created_at"
                )  # no more MultipleObjectsReturned 500

                if verification.is_expired:
                    verification.invalidate()  # soft, not delete
                    return Response(
                        {"error": "Code expired."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                if verification.code != code:
                    return Response(
                        {"error": "Invalid code."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                verification.is_verified = True
                verification.save()

                return Response({"message": "Verified successfully."})

            except (User.DoesNotExist, VerificationRequest.DoesNotExist):
                return Response(
                    {"error": "Invalid request."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class ResetPasswordView(APIView):
    def post(self, request):
        serializer = ResetPasswordSerializer(data=request.data)
        if serializer.is_valid():
            email = serializer.validated_data["email"]
            new_password = serializer.validated_data["new_password"]
            try:
                user = User.objects.get(email=email)
                verification = VerificationRequest.objects.filter(
                    user=user,
                    purpose=VerificationPurpose.RESET_PASSWORD,
                    is_verified=True,
                    invalidated_at__isnull=True,
                ).latest("created_at")

                if verification.is_expired:
                    verification.invalidate()  # soft, not delete
                    return Response(
                        {"error": "Verification expired. Please request a new code."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                user.set_password(new_password)
                user.save()
                verification.invalidate()  # soft-close after use

                return Response({"message": "Password reset successful."})

            except (User.DoesNotExist, VerificationRequest.DoesNotExist):
                return Response(
                    {"error": "Invalid request. Please verify your email first."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class RequestVerificationView(APIView):
    def post(self, request):
        serializer = RequestVerificationSerializer(data=request.data)
        if serializer.is_valid():
            email = serializer.validated_data["email"]
            purpose = serializer.validated_data["purpose"]
            try:
                user = User.objects.get(email=email)

                # Invalidate all previous pending codes for this purpose
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
                    subject="Verify your identity",
                    body=f"Your verification code is: {verification.code}",
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    to=[email],
                )
                msg.attach_alternative(html_content, "text/html")
                msg.send()

            except User.DoesNotExist:
                pass

            return Response(
                {
                    "message": "If that email exists you will receive a verification code."
                }
            )
