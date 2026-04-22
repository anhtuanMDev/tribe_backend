from django.http import JsonResponse
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework_simplejwt.tokens import RefreshToken
from django.contrib.auth import get_user_model
from .models import EmailVerificationCode
from django.template.loader import render_to_string
from django.contrib.auth.tokens import default_token_generator
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.utils.encoding import force_bytes, force_str
from django.core.mail import EmailMultiAlternatives
from django.conf import settings
from .serializers import (
    ForgotPasswordSerializer,
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


class RegisterView(APIView):
    def post(self, request):
        # check if email belongs to a soft-deleted account past 1 week
        email = request.data.get("email")
        if email:
            try:
                existing = User.objects.get(email=email)
                if existing.is_deleted and not existing.is_restorable:
                    existing.permanent_delete()
                elif existing.is_deleted and existing.is_restorable:
                    return Response(
                        {
                            "error": "This account was recently deleted and can still be restored by logging in again."
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            except User.DoesNotExist:
                pass

        serializer = RegisterSerializer(data=request.data)
        if serializer.is_valid():
            user = serializer.save()
            user.is_active = False
            user.save()

            verification = EmailVerificationCode.objects.create(user=user)
            verification.generate()

            html_content = render_to_string(
                "emails/verify_email.html",
                {
                    "verification_code": verification.code,
                    "expiration_minutes": 10,
                    "year": datetime.now().year,
                },
            )

            email = EmailMultiAlternatives(
                subject="Verify your Tribe account",
                body=f"Your verification code is: {verification.code}",
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=[user.email],
            )
            email.attach_alternative(html_content, "text/html")
            email.send()

            return Response(
                {
                    "message": "Registration successful. Check your email for the verification code."
                },
                status=status.HTTP_201_CREATED,
            )
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class VerifyEmailView(APIView):
    def post(self, request):
        serializer = VerifyEmailSerializer(data=request.data)
        if serializer.is_valid():
            email = serializer.validated_data["email"]
            code = serializer.validated_data["code"]
            try:
                user = User.objects.get(email=email)
                verification = EmailVerificationCode.objects.get(user=user)

                # expire code after 10 minutes
                if timezone.now() > verification.created_at + timedelta(minutes=10):
                    return Response(
                        {"error": "Code expired."}, status=status.HTTP_400_BAD_REQUEST
                    )

                if verification.code != code:
                    return Response(
                        {"error": "Invalid code."}, status=status.HTTP_400_BAD_REQUEST
                    )

                user.is_active = True
                user.save()
                verification.delete()

                refresh = RefreshToken.for_user(user)
                return Response(
                    {
                        "token": str(refresh.access_token),
                        "refresh": str(refresh),
                    }
                )
            except (User.DoesNotExist, EmailVerificationCode.DoesNotExist):
                return Response(
                    {"error": "Invalid request."}, status=status.HTTP_400_BAD_REQUEST
                )
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


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
                        {"error": "Invalid credentials"},
                        status=status.HTTP_401_UNAUTHORIZED,
                    )
                if user.is_deleted:
                    if user.is_restorable:
                        user.restore()
                    else:
                        return Response(
                            {"error": "Account has been permanently deleted."},
                            status=status.HTTP_401_UNAUTHORIZED,
                        )
                if not user.is_active:
                    return Response(
                        {"error": "Please verify your email first."},
                        status=status.HTTP_401_UNAUTHORIZED,
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
                    {"error": "Invalid credentials"},
                    status=status.HTTP_401_UNAUTHORIZED,
                )
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class ForgotPasswordView(APIView):
    def post(self, request):
        serializer = ForgotPasswordSerializer(data=request.data)
        if serializer.is_valid():
            email = serializer.validated_data["email"]
            try:
                user = User.objects.get(email=email)
                uid = urlsafe_base64_encode(force_bytes(user.pk))
                token = default_token_generator.make_token(user)
                reset_link = f"tribeapp://reset-password/{uid}/{token}"

                html_content = render_to_string(
                    "emails/forgot_password.html",
                    {
                        "reset_link": reset_link,
                        "expiration_minutes": 10,
                        "year": datetime.now().year,
                    },
                )

                email_msg = EmailMultiAlternatives(
                    subject="Reset your Tribe password",
                    body=f"Click the link to reset your password: {reset_link}",
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    to=[email],
                )
                email_msg.attach_alternative(html_content, "text/html")
                email_msg.send()

            except User.DoesNotExist:
                pass
            return Response(
                {"message": "If that email exists you will receive a reset link."}
            )
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class ResetPasswordView(APIView):
    def post(self, request):
        serializer = ResetPasswordSerializer(data=request.data)
        if serializer.is_valid():
            try:
                uid = force_str(urlsafe_base64_decode(serializer.validated_data["uid"]))
                user = User.objects.get(pk=uid)
                token = serializer.validated_data["token"]
                if default_token_generator.check_token(user, token):
                    user.set_password(serializer.validated_data["new_password"])
                    user.save()
                    return Response({"message": "Password reset successful."})
                return Response(
                    {"error": "Invalid or expired token."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            except (User.DoesNotExist, ValueError):
                return Response(
                    {"error": "Invalid request."}, status=status.HTTP_400_BAD_REQUEST
                )
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
