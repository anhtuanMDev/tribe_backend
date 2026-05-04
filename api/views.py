# views.py
from api.serializers import ActivityTypeSerializer
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
    PostCreateSerializer,
    PostSerializer,
    PostUpdateSerializer,
    PostParticipantSerializer,
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

from math import radians, cos, sin, asin, sqrt
from django.db import transaction
from django.db.models import Case, FloatField, Value, When
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Post, PostParticipant, ActivityType


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


def _haversine(lat1, lon1, lat2, lon2):
    """Return distance in km between two lat/lon points."""
    R = 6371
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return R * 2 * asin(sqrt(a))


def _annotate_distance(queryset, lat, lon):
    """
    Annotate each post with distance_km from the given coordinates.
    Uses Python-side calculation after fetching — good enough for side project.
    For scale, switch to PostGIS + GeoDjango.
    """
    posts = list(queryset)
    for post in posts:
        post.distance_km = round(
            _haversine(lat, lon, float(post.latitude), float(post.longitude)), 2
        )
    posts.sort(key=lambda p: p.distance_km)
    return posts


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
    def post(self, request):
        serializer = VerifyEmailSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        email = serializer.validated_data["email"]
        code = serializer.validated_data["code"]

        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            return Response(
                {"message": "No account found with this email."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if user.is_active:
            return Response(
                {"message": "This account is already verified."},
                status=status.HTTP_409_CONFLICT,
            )

        try:
            verification = VerificationRequest.objects.filter(
                user=user,
                purpose=VerificationPurpose.REGISTER,
                is_verified=False,
                invalidated_at__isnull=True,
            ).latest("created_at")
        except VerificationRequest.DoesNotExist:
            return Response(
                {"message": "No verification code found. Please resend code."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if verification.is_expired:
            verification.invalidate()
            return Response(
                {"message": "Code expired. Please register again to get a new code."},
                status=status.HTTP_410_GONE,
            )

        if verification.code != code:
            return Response(
                {"message": "Invalid code."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user.is_active = True
        user.save()
        verification.is_verified = True
        verification.save()

        refresh = RefreshToken.for_user(user)
        return Response(
            {"token": str(refresh.access_token), "refresh": str(refresh)},
            status=status.HTTP_200_OK,
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
    def post(self, request):
        serializer = ConfirmVerificationSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        email = serializer.validated_data["email"]
        code = serializer.validated_data["code"]
        purpose = serializer.validated_data["purpose"]

        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            return Response(
                {"error": "No account found with this email."},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            verification = VerificationRequest.objects.filter(
                user=user,
                purpose=purpose,
                is_verified=False,
                invalidated_at__isnull=True,
            ).latest("created_at")
        except VerificationRequest.DoesNotExist:
            return Response(
                {"error": "No verification request found. Please request a new code."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if verification.is_expired:
            verification.invalidate()
            return Response(
                {"error": "Code expired."},
                status=status.HTTP_410_GONE,
            )

        if verification.code != code:
            return Response(
                {"error": "Invalid code."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        verification.is_verified = True
        verification.save()

        return Response({"message": "Verified successfully."})


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


class ResendVerificationView(APIView):
    def post(self, request):
        email = request.data.get("email")
        purpose = request.data.get("purpose")

        allowed_purposes = [
            VerificationPurpose.REGISTER,
            VerificationPurpose.RESET_PASSWORD,
            VerificationPurpose.DELETE_ACCOUNT,
            VerificationPurpose.CHANGE_PASSWORD,
        ]

        if not email or purpose not in allowed_purposes:
            return Response(
                {"error": "Invalid request."}, status=status.HTTP_400_BAD_REQUEST
            )

        # Authenticated purposes — user is known, give real errors
        authenticated_purposes = [
            VerificationPurpose.DELETE_ACCOUNT,
            VerificationPurpose.CHANGE_PASSWORD,
        ]

        if purpose in authenticated_purposes:
            try:
                user = User.objects.get(email=email, is_active=True)
                _send_verification(user, purpose)
                return Response({"message": "Verification code sent."})
            except User.DoesNotExist:
                return Response(
                    {"error": "No active account found."},
                    status=status.HTTP_404_NOT_FOUND,
                )

        # Public purposes — silent to prevent enumeration
        try:
            if purpose == VerificationPurpose.REGISTER:
                user = User.objects.get(email=email, is_active=False)
            else:
                user = User.objects.get(email=email, is_active=True)
            _send_verification(user, purpose)
        except User.DoesNotExist:
            pass

        return Response(
            {"message": "If that email exists you will receive a verification code."}
        )


# ---------------------------------------------------------------------------
# Feed & Create
# ---------------------------------------------------------------------------


class PostListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """
        Feed endpoint. Sorts by distance if lat/lon provided.
        Query params:
          lat, lon         — user's current GPS (required for proximity sort)
          activity_type    — filter by ActivityType id
          search           — search title/description/location
          status           — filter by post status (default: open)
        """
        lat = request.query_params.get("lat")
        lon = request.query_params.get("lon")
        activity_type_id = request.query_params.get("activity_type")
        search = request.query_params.get("search")
        post_status = request.query_params.get("status", Post.Status.OPEN)

        qs = (
            Post.objects.filter(status=post_status)
            .select_related("creator", "activity_type")
            .prefetch_related("media", "participants__user")
        )

        if activity_type_id:
            qs = qs.filter(activity_type_id=activity_type_id)

        if search:
            from django.db.models import Q

            qs = qs.filter(
                Q(title__icontains=search)
                | Q(description__icontains=search)
                | Q(location_name__icontains=search)
                | Q(custom_activity__icontains=search)
            )

        if lat and lon:
            try:
                lat, lon = float(lat), float(lon)
                posts = _annotate_distance(qs, lat, lon)
            except ValueError:
                return Response(
                    {"error": "Invalid lat/lon values."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        else:
            # No GPS — fall back to recency
            posts = list(qs.order_by("-created_at"))
            for post in posts:
                post.distance_km = None

        serializer = PostSerializer(posts, many=True, context={"request": request})
        return Response(serializer.data)

    def post(self, request):
        """Create a new post."""
        serializer = PostCreateSerializer(data=request.data)
        if serializer.is_valid():
            post = serializer.save(creator=request.user)
            return Response(
                PostSerializer(post, context={"request": request}).data,
                status=status.HTTP_201_CREATED,
            )
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# ---------------------------------------------------------------------------
# Retrieve, Update, Disband
# ---------------------------------------------------------------------------


class PostDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def _get_post(self, pk):
        try:
            return (
                Post.objects.select_related("creator", "activity_type")
                .prefetch_related("media", "participants__user")
                .get(pk=pk)
            )
        except Post.DoesNotExist:
            return None

    def get(self, request, pk):
        post = self._get_post(pk)
        if not post:
            return Response(
                {"error": "Post not found."}, status=status.HTTP_404_NOT_FOUND
            )
        serializer = PostSerializer(post, context={"request": request})
        return Response(serializer.data)

    def patch(self, request, pk):
        post = self._get_post(pk)
        if not post:
            return Response(
                {"error": "Post not found."}, status=status.HTTP_404_NOT_FOUND
            )

        if post.creator != request.user:
            return Response(
                {"error": "Only the host can edit this post."},
                status=status.HTTP_403_FORBIDDEN,
            )

        if post.status in (Post.Status.DISBANDED, Post.Status.EXPIRED):
            return Response(
                {"error": "Cannot edit a disbanded or expired post."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = PostUpdateSerializer(post, data=request.data, partial=True)
        if serializer.is_valid():
            updated_post = serializer.save()
            return Response(
                PostSerializer(updated_post, context={"request": request}).data
            )
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, pk):
        """Disband the event — soft close, not hard delete."""
        post = self._get_post(pk)
        if not post:
            return Response(
                {"error": "Post not found."}, status=status.HTTP_404_NOT_FOUND
            )

        if post.creator != request.user:
            return Response(
                {"error": "Only the host can disband this post."},
                status=status.HTTP_403_FORBIDDEN,
            )

        if post.status == Post.Status.DISBANDED:
            return Response(
                {"error": "Post is already disbanded."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        post.disband()
        return Response({"message": "Event disbanded."})


# ---------------------------------------------------------------------------
# Join Request
# ---------------------------------------------------------------------------


class PostJoinView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        try:
            post = Post.objects.get(pk=pk)
        except Post.DoesNotExist:
            return Response(
                {"error": "Post not found."}, status=status.HTTP_404_NOT_FOUND
            )

        if post.creator == request.user:
            return Response(
                {"error": "You cannot join your own post."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if post.status not in (Post.Status.OPEN,):
            return Response(
                {"error": "This post is not open for joining."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if post.is_expired:
            return Response(
                {"error": "This event has already started."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        existing = PostParticipant.objects.filter(post=post, user=request.user).first()
        if existing:
            if existing.status == PostParticipant.Status.PENDING:
                return Response(
                    {"error": "You already have a pending request."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if existing.status == PostParticipant.Status.RESERVED:
                return Response(
                    {"error": "You are already in this event."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if existing.status in (
                PostParticipant.Status.REJECTED,
                PostParticipant.Status.CANCELLED,
            ):
                # Allow re-request
                existing.status = PostParticipant.Status.PENDING
                existing.requested_at = timezone.now()
                existing.resolved_at = None
                existing.save()
                return Response(
                    {"message": "Join request sent."}, status=status.HTTP_200_OK
                )

        if post.is_unlimited:
            # Unlimited — auto approve
            PostParticipant.objects.create(
                post=post,
                user=request.user,
                status=PostParticipant.Status.RESERVED,
                resolved_at=timezone.now(),
            )
            return Response(
                {"message": "You have joined the event."},
                status=status.HTTP_201_CREATED,
            )
        else:
            # Limited — create pending request
            PostParticipant.objects.create(
                post=post,
                user=request.user,
                status=PostParticipant.Status.PENDING,
            )
            return Response(
                {"message": "Join request sent."}, status=status.HTTP_201_CREATED
            )


# ---------------------------------------------------------------------------
# Host: Approve / Reject
# ---------------------------------------------------------------------------


class PostApproveView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk, user_id):
        try:
            post = Post.objects.get(pk=pk)
        except Post.DoesNotExist:
            return Response(
                {"error": "Post not found."}, status=status.HTTP_404_NOT_FOUND
            )

        if post.creator != request.user:
            return Response(
                {"error": "Only the host can approve requests."},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            participant = PostParticipant.objects.get(
                post=post,
                user_id=user_id,
                status=PostParticipant.Status.PENDING,
            )
        except PostParticipant.DoesNotExist:
            return Response(
                {"error": "No pending request from this user."},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            with transaction.atomic():
                # Lock the post row to prevent race conditions
                post = Post.objects.select_for_update().get(pk=pk)
                participant.approve()
        except ValueError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({"message": "Participant approved."})


class PostRejectView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk, user_id):
        try:
            post = Post.objects.get(pk=pk)
        except Post.DoesNotExist:
            return Response(
                {"error": "Post not found."}, status=status.HTTP_404_NOT_FOUND
            )

        if post.creator != request.user:
            return Response(
                {"error": "Only the host can reject requests."},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            participant = PostParticipant.objects.get(
                post=post,
                user_id=user_id,
                status=PostParticipant.Status.PENDING,
            )
        except PostParticipant.DoesNotExist:
            return Response(
                {"error": "No pending request from this user."},
                status=status.HTTP_404_NOT_FOUND,
            )

        participant.reject()
        return Response({"message": "Participant rejected."})


# ---------------------------------------------------------------------------
# Participant: Cancel own request
# ---------------------------------------------------------------------------


class PostCancelView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        try:
            post = Post.objects.get(pk=pk)
        except Post.DoesNotExist:
            return Response(
                {"error": "Post not found."}, status=status.HTTP_404_NOT_FOUND
            )

        try:
            participant = PostParticipant.objects.get(
                post=post,
                user=request.user,
            )
        except PostParticipant.DoesNotExist:
            return Response(
                {"error": "You are not a participant of this post."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if participant.status in (
            PostParticipant.Status.REJECTED,
            PostParticipant.Status.CANCELLED,
        ):
            return Response(
                {"error": "Nothing to cancel."}, status=status.HTTP_400_BAD_REQUEST
            )

        participant.cancel()
        return Response({"message": "You have left the event."})


# ---------------------------------------------------------------------------
# Host: View pending requests
# ---------------------------------------------------------------------------


class PostPendingRequestsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        try:
            post = Post.objects.get(pk=pk)
        except Post.DoesNotExist:
            return Response(
                {"error": "Post not found."}, status=status.HTTP_404_NOT_FOUND
            )

        if post.creator != request.user:
            return Response(
                {"error": "Only the host can view pending requests."},
                status=status.HTTP_403_FORBIDDEN,
            )

        pending = (
            PostParticipant.objects.filter(
                post=post,
                status=PostParticipant.Status.PENDING,
            )
            .select_related("user")
            .order_by("requested_at")
        )

        serializer = PostParticipantSerializer(pending, many=True)
        return Response(serializer.data)


class ActivityTypeListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        activities = ActivityType.objects.all()
        serializer = ActivityTypeSerializer(activities, many=True)
        return Response(serializer.data)
