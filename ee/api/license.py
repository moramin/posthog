from datetime import timedelta

from django.db.models import QuerySet
from django.utils.timezone import now

import posthoganalytics
from rest_framework import mixins, request, serializers, viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from posthog.cloud_utils import is_cloud
from posthog.event_usage import groups
from posthog.models.organization import Organization
from posthog.models.team import Team
from posthog.permissions import IsStaffUser, TimeSensitiveActionPermission

from ee.models.license import License


class LicenseSerializer(serializers.ModelSerializer):
    class Meta:
        model = License
        fields = [
            "id",
            "plan",
            "key",
            "valid_until",
            "created_at",
        ]
        read_only_fields = ["plan", "valid_until"]
        extra_kwargs = {"key": {"write_only": True}}

    def validate(self, data):
        # Self-hosted, single-tenant fork: license activation is local-only and always
        # grants the enterprise plan, never contacting license.posthog.com.
        user = self.context["request"].user
        posthoganalytics.capture(
            "license key activation success",
            distinct_id=user.distinct_id,
            properties={},
            groups=groups(user.current_organization, user.current_team),
        )
        data["valid_until"] = now() + timedelta(days=365 * 100)
        data["plan"] = License.ENTERPRISE_PLAN
        return data


class LicenseViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    queryset = License.objects.all()
    serializer_class = LicenseSerializer
    permission_classes = [IsAuthenticated, IsStaffUser, TimeSensitiveActionPermission]

    def get_queryset(self) -> QuerySet:
        if is_cloud():
            return License.objects.none()

        return super().get_queryset()

    def destroy(self, request: request.Request, *args, **kwargs) -> Response:
        # Self-hosted, single-tenant fork: license deactivation is local-only and never
        # contacts license.posthog.com.
        license = self.get_object()

        has_another_valid_license = License.objects.filter(valid_until__gte=now()).exclude(pk=license.pk).exists()
        if not has_another_valid_license:
            teams = Team.objects.exclude(is_demo=True).order_by("pk")[1:]
            for team in teams:
                team.delete()

            #  delete any organization where we've deleted all teams
            # there is no way in the interface to create multiple organizations so we won't bother informing people that this is happening
            for organization in Organization.objects.all():
                if organization.teams.count() == 0:
                    organization.delete()

        license.delete()

        return Response({"ok": True})
