from rest_framework import serializers

from .models import Asset, CheckOut, Employee, OverdueNotice


class AssetSerializer(serializers.ModelSerializer):
    class Meta:
        model = Asset
        fields = [
            "id",
            "asset_tag",
            "name",
            "category",
            "status",
            "purchase_date",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "status", "created_at", "updated_at"]


class CurrentHolderSerializer(serializers.Serializer):
    employee_code = serializers.CharField()
    full_name = serializers.CharField()


class AssetDetailSerializer(AssetSerializer):
    current_holder = serializers.SerializerMethodField()

    class Meta(AssetSerializer.Meta):
        fields = AssetSerializer.Meta.fields + ["current_holder"]

    def get_current_holder(self, obj):
        # obj.status is checked first to avoid an extra query for the
        # common "available" case.
        if obj.status != Asset.Status.CHECKED_OUT:
            return None
        open_checkout = obj.checkouts.filter(returned_at__isnull=True).select_related(
            "employee"
        ).first()
        if not open_checkout:
            return None
        return CurrentHolderSerializer(
            {
                "employee_code": open_checkout.employee.employee_code,
                "full_name": open_checkout.employee.full_name,
            }
        ).data


class EmployeeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Employee
        fields = ["id", "employee_code", "full_name", "email", "is_active"]


class CheckOutCreateSerializer(serializers.Serializer):
    asset_tag = serializers.CharField()
    employee_code = serializers.CharField()
    due_at = serializers.DateTimeField()


class CheckOutReturnSerializer(serializers.Serializer):
    condition_note = serializers.CharField(required=False, allow_blank=True, default="")
    needs_maintenance = serializers.BooleanField(required=False, default=False)


class CheckOutSerializer(serializers.ModelSerializer):
    asset_tag = serializers.CharField(source="asset.asset_tag", read_only=True)
    employee_code = serializers.CharField(source="employee.employee_code", read_only=True)

    class Meta:
        model = CheckOut
        fields = [
            "id",
            "asset",
            "asset_tag",
            "employee",
            "employee_code",
            "checked_out_at",
            "due_at",
            "returned_at",
            "condition_note",
        ]
        read_only_fields = fields


class EmployeeSummarySerializer(serializers.Serializer):
    employee_code = serializers.CharField()
    lifetime_checkout_count = serializers.IntegerField()
    currently_held_count = serializers.IntegerField()
    currently_overdue_count = serializers.IntegerField()
    mean_hold_duration_days = serializers.FloatField(allow_null=True)


class OverdueReportRowSerializer(serializers.Serializer):
    checkout_id = serializers.IntegerField()
    asset_name = serializers.CharField()
    asset_tag = serializers.CharField()
    employee_code = serializers.CharField()
    employee_name = serializers.CharField()
    due_at = serializers.DateTimeField()
    days_overdue = serializers.IntegerField()
