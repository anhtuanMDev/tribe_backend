from api.constants import VerificationPurpose
from rest_framework import serializers
from api.models import ActivityType, Post, PostMedia, PostParticipant
from django.contrib.auth import get_user_model

User = get_user_model()


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=8)

    class Meta:
        model = User
        fields = ("email", "username", "password")

    def create(self, validated_data):
        return User.objects.create_user(**validated_data)


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)


class ForgotPasswordSerializer(serializers.Serializer):
    email = serializers.EmailField()


class VerifyEmailSerializer(serializers.Serializer):
    email = serializers.EmailField()
    code = serializers.CharField(max_length=6)


class RequestVerificationSerializer(serializers.Serializer):
    email = serializers.EmailField()
    purpose = serializers.ChoiceField(
        choices=[
            VerificationPurpose.RESET_PASSWORD,
            VerificationPurpose.DELETE_ACCOUNT,
            VerificationPurpose.CHANGE_PASSWORD,
        ]
    )


class ConfirmVerificationSerializer(serializers.Serializer):
    email = serializers.EmailField()
    code = serializers.CharField(max_length=6)
    purpose = serializers.ChoiceField(
        choices=[
            VerificationPurpose.RESET_PASSWORD,
            VerificationPurpose.DELETE_ACCOUNT,
            VerificationPurpose.CHANGE_PASSWORD,
        ]
    )


class ResetPasswordSerializer(serializers.Serializer):
    email = serializers.EmailField()
    new_password = serializers.CharField(min_length=8, write_only=True)


class ChangePasswordSerializer(serializers.Serializer):
    current_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(min_length=8, write_only=True)


class ActivityTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = ActivityType
        fields = ("id", "name", "icon_key")


class PostMediaSerializer(serializers.ModelSerializer):
    class Meta:
        model = PostMedia
        fields = ("id", "file_url", "order")


class ParticipantUserSerializer(serializers.ModelSerializer):
    """Minimal user info shown on participant list."""

    class Meta:
        model = User
        fields = ("id", "username")


class PostParticipantSerializer(serializers.ModelSerializer):
    user = ParticipantUserSerializer(read_only=True)

    class Meta:
        model = PostParticipant
        fields = ("id", "user", "status", "requested_at", "resolved_at")


class PostSerializer(serializers.ModelSerializer):
    """Read serializer — used for list and detail responses."""

    creator = ParticipantUserSerializer(read_only=True)
    activity_type = ActivityTypeSerializer(read_only=True)
    media = PostMediaSerializer(many=True, read_only=True)
    participants = serializers.SerializerMethodField()
    filled_count = serializers.IntegerField(read_only=True)
    remaining_slots = serializers.SerializerMethodField()
    # People the requesting user has shared a past event with, among participants
    known_participants = serializers.SerializerMethodField()
    distance_km = serializers.SerializerMethodField()

    class Meta:
        model = Post
        fields = (
            "id",
            "creator",
            "title",
            "description",
            "activity_type",
            "custom_activity",
            "location_name",
            "latitude",
            "longitude",
            "scheduled_start",
            "scheduled_end",
            "plan_vibe",
            "open_slots",
            "reopen_on_dropout",
            "status",
            "filled_count",
            "remaining_slots",
            "participants",
            "known_participants",
            "media",
            "distance_km",
            "created_at",
        )

    def get_remaining_slots(self, obj):
        return obj.remaining_slots  # None if unlimited

    def get_participants(self, obj):
        """Only show accepted (reserved) participants publicly."""
        reserved = obj.participants.filter(
            status=PostParticipant.Status.RESERVED
        ).select_related("user")
        return PostParticipantSerializer(reserved, many=True).data

    def get_known_participants(self, obj):
        """
        Among reserved participants on this post, who has shared
        a past event with the requesting user?
        Returns list of user ids the current user has co-participated with.
        """
        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return []

        current_user = request.user

        # Posts the current user has been reserved in (excluding this post)
        past_post_ids = (
            PostParticipant.objects.filter(
                user=current_user,
                status=PostParticipant.Status.RESERVED,
            )
            .exclude(post=obj)
            .values_list("post_id", flat=True)
        )

        # Users who were also reserved in those past posts
        known_user_ids = set(
            PostParticipant.objects.filter(
                post_id__in=past_post_ids,
                status=PostParticipant.Status.RESERVED,
            )
            .exclude(user=current_user)
            .values_list("user_id", flat=True)
        )

        # Intersect with this post's reserved participants
        known_here = obj.participants.filter(
            status=PostParticipant.Status.RESERVED,
            user_id__in=known_user_ids,
        ).select_related("user")

        return PostParticipantSerializer(known_here, many=True).data

    def get_distance_km(self, obj):
        """
        Distance is annotated on the queryset in the view.
        Falls back to None if not annotated (e.g. no GPS provided).
        """
        return getattr(obj, "distance_km", None)


class PostCreateSerializer(serializers.ModelSerializer):
    """Write serializer — used for POST /posts/"""

    media_urls = serializers.ListField(
        child=serializers.URLField(),
        write_only=True,
        required=False,
        max_length=3,  # max 3 photos per your UI
    )

    class Meta:
        model = Post
        fields = (
            "title",
            "description",
            "activity_type",
            "custom_activity",
            "location_name",
            "latitude",
            "longitude",
            "scheduled_start",
            "scheduled_end",
            "plan_vibe",
            "open_slots",
            "reopen_on_dropout",
            "media_urls",
        )

    def validate(self, attrs):
        activity_type = attrs.get("activity_type")
        custom_activity = attrs.get("custom_activity")

        if activity_type and activity_type.name == "Other" and not custom_activity:
            raise serializers.ValidationError(
                {"custom_activity": "Required when activity type is 'Other'."}
            )

        if activity_type and activity_type.name != "Other" and custom_activity:
            attrs["custom_activity"] = None  # ignore custom if not Other

        if attrs.get("scheduled_start") and attrs.get("scheduled_end"):
            if attrs["scheduled_end"] <= attrs["scheduled_start"]:
                raise serializers.ValidationError(
                    {"scheduled_end": "End time must be after start time."}
                )

        return attrs

    def create(self, validated_data):
        media_urls = validated_data.pop("media_urls", [])
        post = Post.objects.create(**validated_data)

        for i, url in enumerate(media_urls):
            PostMedia.objects.create(post=post, file_url=url, order=i)

        return post


class PostUpdateSerializer(serializers.ModelSerializer):
    """Write serializer — used for PATCH /posts/<id>/"""

    media_urls = serializers.ListField(
        child=serializers.URLField(),
        write_only=True,
        required=False,
        max_length=3,
    )

    class Meta:
        model = Post
        fields = (
            "title",
            "description",
            "activity_type",
            "custom_activity",
            "location_name",
            "latitude",
            "longitude",
            "scheduled_start",
            "scheduled_end",
            "plan_vibe",
            "open_slots",
            "reopen_on_dropout",
            "media_urls",
        )

    def validate(self, attrs):
        # Re-use same validation as create
        activity_type = attrs.get(
            "activity_type", getattr(self.instance, "activity_type", None)
        )
        custom_activity = attrs.get("custom_activity", self.instance.custom_activity)

        if activity_type and activity_type.name == "Other" and not custom_activity:
            raise serializers.ValidationError(
                {"custom_activity": "Required when activity type is 'Other'."}
            )

        start = attrs.get("scheduled_start", self.instance.scheduled_start)
        end = attrs.get("scheduled_end", self.instance.scheduled_end)
        if end <= start:
            raise serializers.ValidationError(
                {"scheduled_end": "End time must be after start time."}
            )

        return attrs

    def update(self, instance, validated_data):
        media_urls = validated_data.pop("media_urls", None)

        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        # If media_urls provided, replace all existing media
        if media_urls is not None:
            instance.media.all().delete()
            for i, url in enumerate(media_urls):
                PostMedia.objects.create(post=instance, file_url=url, order=i)

        return instance


class JoinRequestSerializer(serializers.Serializer):
    """Used for POST /posts/<id>/join/ — no body needed but kept for extensibility."""

    pass
