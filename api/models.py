from api.constants import PURPOSE_CHOICES
from datetime import timedelta
from django.contrib.auth.models import AbstractUser
from django.db import models
import random
from django.utils import timezone
from django.conf import settings


class User(AbstractUser):
    email = models.EmailField(unique=True)
    username = models.CharField(max_length=150, unique=False)
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["username"]
    deleted_at = models.DateTimeField(null=True, blank=True)

    @property
    def is_deleted(self):
        return self.deleted_at is not None

    @property
    def is_restorable(self):
        if not self.deleted_at:
            return False
        return timezone.now() < self.deleted_at + timedelta(weeks=1)

    def soft_delete(self):
        self.deleted_at = timezone.now()
        self.is_active = False
        self.save()

    def restore(self):
        self.deleted_at = None
        self.is_active = True
        self.save()

    def permanent_delete(self):
        self.email = f"deleted_{self.id}@deleted.tribe"
        self.is_active = False
        self.save()


class EmailVerificationCode(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    code = models.CharField(max_length=6)
    created_at = models.DateTimeField(auto_now_add=True)

    def generate(self):
        self.code = str(random.randint(100000, 999999))
        self.save()


class VerificationRequest(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    code = models.CharField(max_length=6)
    purpose = models.CharField(max_length=50, choices=PURPOSE_CHOICES)
    is_verified = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    invalidated_at = models.DateTimeField(null=True, blank=True)  # soft-cancel

    def generate(self):
        self.code = str(random.randint(100000, 999999))
        self.save()

    def invalidate(self):
        self.invalidated_at = timezone.now()
        self.save()

    @property
    def is_expired(self):
        return timezone.now() > self.created_at + timedelta(minutes=10)

    @property
    def is_valid(self):
        """Single source of truth for usability."""
        return (
            not self.is_verified and not self.is_expired and self.invalidated_at is None
        )


class ActivityType(models.Model):
    name = models.CharField(max_length=100, unique=True)
    icon_key = models.CharField(max_length=50)  # maps to icon in frontend

    def __str__(self):
        return self.name

    class Meta:
        ordering = ["name"]


class Post(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        FULL = "full", "Full"
        CLOSED = "closed", "Closed"
        EXPIRED = "expired", "Expired"
        DISBANDED = "disbanded", "Disbanded"

    class PlanVibe(models.TextChoices):
        CASUAL = "casual", "Casual"
        DEMO = "demo", "Demo"
        COMPETITIVE = "competitive", "Competitive"

    creator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="posts",
    )
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)

    activity_type = models.ForeignKey(
        ActivityType,
        on_delete=models.PROTECT,
        related_name="posts",
    )
    # Only populated when activity_type.name == "Other"
    custom_activity = models.CharField(max_length=100, blank=True, null=True)

    location_name = models.CharField(max_length=255)
    latitude = models.DecimalField(max_digits=9, decimal_places=6)
    longitude = models.DecimalField(max_digits=9, decimal_places=6)

    scheduled_start = models.DateTimeField()
    scheduled_end = models.DateTimeField()

    plan_vibe = models.CharField(
        max_length=20,
        choices=PlanVibe.choices,
        default=PlanVibe.CASUAL,
    )

    # None = unlimited (just join, no approval needed)
    # Integer = limited slots, requires host approval
    open_slots = models.PositiveIntegerField(null=True, blank=True)
    reopen_on_dropout = models.BooleanField(default=False)

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.OPEN,
    )

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.title} by {self.creator}"

    @property
    def is_unlimited(self):
        return self.open_slots is None

    @property
    def filled_count(self):
        return self.participants.filter(status=PostParticipant.Status.RESERVED).count()

    @property
    def remaining_slots(self):
        if self.is_unlimited:
            return None
        return self.open_slots - self.filled_count

    @property
    def is_expired(self):
        return timezone.now() > self.scheduled_start

    def disband(self):
        self.status = Post.Status.DISBANDED
        self.save()

    class Meta:
        ordering = ["-created_at"]


class PostMedia(models.Model):
    post = models.ForeignKey(
        Post,
        on_delete=models.CASCADE,
        related_name="media",
    )
    file_url = models.URLField()  # Cloudinary URL
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["order"]


class PostParticipant(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"  # requested, not yet approved
        RESERVED = "reserved", "Reserved"  # approved, counts against capacity
        REJECTED = "rejected", "Rejected"
        CANCELLED = "cancelled", "Cancelled"

    post = models.ForeignKey(
        Post,
        on_delete=models.CASCADE,
        related_name="participants",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="participations",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    requested_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    def approve(self):
        """
        Approve a pending request. Checks capacity before approving.
        Raises ValueError if post is full.
        """
        post = self.post

        if not post.is_unlimited:
            if post.remaining_slots <= 0:
                raise ValueError("Post is already full.")

        self.status = PostParticipant.Status.RESERVED
        self.resolved_at = timezone.now()
        self.save()

        # Auto-close post if now full
        if not post.is_unlimited and post.remaining_slots == 0:
            post.status = Post.Status.FULL
            post.save()

    def reject(self):
        self.status = PostParticipant.Status.REJECTED
        self.resolved_at = timezone.now()
        self.save()

    def cancel(self):
        was_reserved = self.status == PostParticipant.Status.RESERVED
        self.status = PostParticipant.Status.CANCELLED
        self.resolved_at = timezone.now()
        self.save()

        # Re-open slot if host opted in
        if was_reserved and not self.post.is_unlimited:
            if self.post.reopen_on_dropout and self.post.status == Post.Status.FULL:
                self.post.status = Post.Status.OPEN
                self.post.save()

    class Meta:
        # One request per user per post
        unique_together = ("post", "user")
        ordering = ["requested_at"]
