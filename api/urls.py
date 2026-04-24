from api.views import health_check
from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView
from .views import (
    RegisterView,
    LoginView,
    VerifyEmailView,
    RequestVerificationView,
    ConfirmVerificationView,
    ResetPasswordView,
)

urlpatterns = [
    path("health/", health_check),
    path("register/", RegisterView.as_view()),
    path("login/", LoginView.as_view()),
    path("verify-email/", VerifyEmailView.as_view()),
    path("request-verification/", RequestVerificationView.as_view()),
    path("confirm-verification/", ConfirmVerificationView.as_view()),
    path("reset-password/", ResetPasswordView.as_view()),
    path("api/token/refresh/", TokenRefreshView.as_view()),
]
