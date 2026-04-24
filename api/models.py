from api.constants import PURPOSE_CHOICES
from datetime import timedelta
from django.contrib.auth.models import AbstractUser
from django.db import models
import random
from django.utils import timezone


class User(AbstractUser):
    email = models.EmailField(unique=True)
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

    def generate(self):
        self.code = str(random.randint(100000, 999999))
        self.save()

    @property
    def is_expired(self):
        return timezone.now() > self.created_at + timedelta(minutes=10)
