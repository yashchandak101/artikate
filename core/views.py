from datetime import timedelta

import django_filters
from django.db import connection, transaction
from django.db.models import Avg, Count, DurationField, ExpressionWrapper, F, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Asset, CheckOut, Employee
from .serializers import (
    AssetDetailSerializer,
    AssetSerializer,
    CheckOutCreateSerializer,
    CheckOutReturnSerializer,
    CheckOutSerializer,
    EmployeeSummarySerializer,
    OverdueReportRowSerializer,
)

MAX_OPEN_CHECKOUTS_PER_EMPLOYEE = 3
MAX_DUE_AT_DAYS_AHEAD = 30


class AssetFilter(django_filters.FilterSet):
    class Meta:
        model = Asset
        fields = ["status", "category"]


class AssetViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    queryset = Asset.objects.all()
    filterset_class = AssetFilter
    search_fields = ["name", "asset_tag"]

    def get_serializer_class(self):
        if self.action == "retrieve":
            return AssetDetailSerializer
        return AssetSerializer


class CheckOutViewSet(
    mixins.CreateModelMixin,
    mixins.RetrieveModelMixin,
    mixins.ListModelMixin,
    viewsets.GenericViewSet,
):
    queryset = CheckOut.objects.select_related("asset", "employee").all()
    serializer_class = CheckOutSerializer

    def create(self, request, *args, **kwargs):
        payload = CheckOutCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        asset_tag = payload.validated_data["asset_tag"]
        employee_code = payload.validated_data["employee_code"]
        due_at = payload.validated_data["due_at"]

        # 404s first: we can't reason about state for things that don't exist.
        asset = get_object_or_404(Asset, asset_tag=asset_tag)
        employee = get_object_or_404(Employee, employee_code=employee_code)

        # 400s: request-shape / policy violations that don't depend on
        # concurrent state.
        if not employee.is_active:
            raise ValidationError({"detail": "employee is not active"})

        now = timezone.now()
        if due_at <= now:
            raise ValidationError({"detail": "due_at must be in the future"})
        if due_at > now + timedelta(days=MAX_DUE_AT_DAYS_AHEAD):
            raise ValidationError(
                {"detail": f"due_at cannot be more than {MAX_DUE_AT_DAYS_AHEAD} days ahead"}
            )

        # 409s: concurrency-sensitive checks. Everything from here on
        # happens inside one transaction with the asset row locked, so a
        # second concurrent request for the same asset blocks on the
        # SELECT ... FOR UPDATE until this transaction commits/rolls back,
        # then re-reads the now-updated status and correctly gets 409.
        with transaction.atomic():
            asset_locked = Asset.objects.select_for_update().get(pk=asset.pk)
            if asset_locked.status != Asset.Status.AVAILABLE:
                return Response(
                    {"detail": "asset is not available"}, status=status.HTTP_409_CONFLICT
                )

            employee_locked = Employee.objects.select_for_update().get(pk=employee.pk)
            open_count = CheckOut.objects.filter(
                employee=employee_locked, returned_at__isnull=True
            ).count()
            if open_count >= MAX_OPEN_CHECKOUTS_PER_EMPLOYEE:
                return Response(
                    {"detail": "employee already has 3 open check-outs"},
                    status=status.HTTP_409_CONFLICT,
                )

            checkout = CheckOut.objects.create(
                asset=asset_locked,
                employee=employee_locked,
                due_at=due_at,
            )
            asset_locked.status = Asset.Status.CHECKED_OUT
            asset_locked.save(update_fields=["status", "updated_at"])

        serializer = self.get_serializer(checkout)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="return")
    def return_checkout(self, request, pk=None):
        payload = CheckOutReturnSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        checkout = get_object_or_404(CheckOut, pk=pk)

        with transaction.atomic():
            checkout_locked = CheckOut.objects.select_for_update().get(pk=checkout.pk)
            if checkout_locked.returned_at is not None:
                return Response(
                    {"detail": "check-out has already been returned"},
                    status=status.HTTP_409_CONFLICT,
                )

            checkout_locked.returned_at = timezone.now()
            checkout_locked.condition_note = payload.validated_data["condition_note"]
            checkout_locked.save(update_fields=["returned_at", "condition_note"])

            asset = Asset.objects.select_for_update().get(pk=checkout_locked.asset_id)
            asset.status = (
                Asset.Status.MAINTENANCE
                if payload.validated_data["needs_maintenance"]
                else Asset.Status.AVAILABLE
            )
            asset.save(update_fields=["status", "updated_at"])

        serializer = CheckOutSerializer(checkout_locked)
        return Response(serializer.data, status=status.HTTP_200_OK)


class EmployeeSummaryView(APIView):
    def get(self, request, employee_code):
        employee = get_object_or_404(Employee, employee_code=employee_code)
        now = timezone.now()

        hold_duration = ExpressionWrapper(
            F("returned_at") - F("checked_out_at"), output_field=DurationField()
        )

        aggregates = CheckOut.objects.filter(employee=employee).aggregate(
            lifetime_checkout_count=Count("id"),
            currently_held_count=Count("id", filter=Q(returned_at__isnull=True)),
            currently_overdue_count=Count(
                "id", filter=Q(returned_at__isnull=True, due_at__lt=now)
            ),
            mean_hold_duration=Avg(hold_duration, filter=Q(returned_at__isnull=False)),
        )

        mean_duration = aggregates["mean_hold_duration"]
        mean_hold_duration_days = (
            round(mean_duration.total_seconds() / 86400, 2) if mean_duration else None
        )

        data = {
            "employee_code": employee.employee_code,
            "lifetime_checkout_count": aggregates["lifetime_checkout_count"],
            "currently_held_count": aggregates["currently_held_count"],
            "currently_overdue_count": aggregates["currently_overdue_count"],
            "mean_hold_duration_days": mean_hold_duration_days,
        }
        return Response(EmployeeSummarySerializer(data).data)


class OverdueReportView(APIView):
    def get(self, request):
        now = timezone.now()
        # select_related pulls asset + employee columns in the same query
        # via SQL JOINs, so rendering N rows costs 1 query, not N+1.
        queryset = (
            CheckOut.objects.filter(returned_at__isnull=True, due_at__lt=now)
            .select_related("asset", "employee")
            .order_by("due_at")
        )

        rows = [
            {
                "checkout_id": c.id,
                "asset_name": c.asset.name,
                "asset_tag": c.asset.asset_tag,
                "employee_code": c.employee.employee_code,
                "employee_name": c.employee.full_name,
                "due_at": c.due_at,
                "days_overdue": (now - c.due_at).days,
            }
            for c in queryset
        ]
        serializer = OverdueReportRowSerializer(rows, many=True)
        return Response({"count": len(rows), "results": serializer.data})


class HealthView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        db_ok = True
        db_error = None
        try:
            connection.ensure_connection()
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
        except Exception as exc:  # pragma: no cover - defensive
            db_ok = False
            db_error = str(exc)

        body = {"status": "ok" if db_ok else "error", "database": db_ok}
        if db_error:
            body["database_error"] = db_error
        return Response(body, status=status.HTTP_200_OK if db_ok else status.HTTP_503_SERVICE_UNAVAILABLE)
