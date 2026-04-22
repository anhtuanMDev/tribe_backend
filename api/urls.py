from api.views import health_check
from django.urls import path
from .views import (
    RegisterView,
    LoginView,
    ForgotPasswordView,
    ResetPasswordView,
    VerifyEmailView,
)
from rest_framework_simplejwt.views import TokenRefreshView

urlpatterns = [
    path("health/", health_check),
    path("register/", RegisterView.as_view()),
    path("login/", LoginView.as_view()),
    path("verify-email/", VerifyEmailView.as_view()),
    path("forgot-password/", ForgotPasswordView.as_view()),
    path("reset-password/", ResetPasswordView.as_view()),
    path("api/token/refresh/", TokenRefreshView.as_view()),
]
