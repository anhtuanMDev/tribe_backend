from api.views import ResendVerificationView
from api.views import health_check
from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView
from .views import (
    RegisterView,
    LoginView,
    RequestVerificationView,
    ConfirmVerificationView,
    ResetPasswordView,
    VerifyEmailView,
    ChangePasswordView,
    DeleteAccountView,
    PostListCreateView,
    PostDetailView,
    PostJoinView,
    PostCancelView,
    PostPendingRequestsView,
    PostApproveView,
    PostRejectView,
)

urlpatterns = [
    path("health/", health_check),
    path("register/", RegisterView.as_view()),
    path("login/", LoginView.as_view()),
    path("verify-email/", VerifyEmailView.as_view()),
    path("request-verification/", RequestVerificationView.as_view()),
    path("confirm-verification/", ConfirmVerificationView.as_view()),
    path("reset-password/", ResetPasswordView.as_view()),
    path("change-password/", ChangePasswordView.as_view()),
    path("delete-account/", DeleteAccountView.as_view()),
    path("api/token/refresh/", TokenRefreshView.as_view()),
    path("resend-verification/", ResendVerificationView.as_view()),
    path("posts/", PostListCreateView.as_view()),  # GET feed, POST create
    path(
        "posts/<int:pk>/", PostDetailView.as_view()
    ),  # GET detail, PATCH edit, DELETE disband
    path("posts/<int:pk>/join/", PostJoinView.as_view()),  # POST join request
    path("posts/<int:pk>/cancel/", PostCancelView.as_view()),  # POST cancel own request
    path(
        "posts/<int:pk>/requests/", PostPendingRequestsView.as_view()
    ),  # GET pending requests (host only)
    path(
        "posts/<int:pk>/approve/<int:user_id>/", PostApproveView.as_view()
    ),  # POST approve participant
    path(
        "posts/<int:pk>/reject/<int:user_id>/", PostRejectView.as_view()
    ),  # POST reject participant
]
