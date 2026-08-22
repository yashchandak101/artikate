from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework import status

from core.models import Asset, CheckOut

pytestmark = pytest.mark.django_db


class TestEmployeeSummary:
    def test_summary_counts_and_mean_duration(self, auth_client, make_asset, make_employee):
        employee = make_employee(code="EMP-300")
        now = timezone.now()

        # one open, on-time-eligible check-out (not overdue)
        a1 = make_asset(tag="AST-300", status=Asset.Status.CHECKED_OUT)
        c1 = CheckOut.objects.create(asset=a1, employee=employee, due_at=now + timedelta(days=5))
        CheckOut.objects.filter(pk=c1.pk).update(checked_out_at=now - timedelta(days=1))

        # one open, overdue check-out
        a2 = make_asset(tag="AST-301", status=Asset.Status.CHECKED_OUT)
        c2 = CheckOut.objects.create(asset=a2, employee=employee, due_at=now - timedelta(days=2))
        CheckOut.objects.filter(pk=c2.pk).update(checked_out_at=now - timedelta(days=10))

        # two returned check-outs: held 2 days and 4 days -> mean = 3.0
        a3 = make_asset(tag="AST-302")
        c3 = CheckOut.objects.create(asset=a3, employee=employee, due_at=now + timedelta(days=10))
        CheckOut.objects.filter(pk=c3.pk).update(
            checked_out_at=now - timedelta(days=10), returned_at=now - timedelta(days=8)
        )

        a4 = make_asset(tag="AST-303")
        c4 = CheckOut.objects.create(asset=a4, employee=employee, due_at=now + timedelta(days=10))
        CheckOut.objects.filter(pk=c4.pk).update(
            checked_out_at=now - timedelta(days=20), returned_at=now - timedelta(days=16)
        )

        resp = auth_client.get(f"/api/v1/employees/{employee.employee_code}/summary/")
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()

        assert data["lifetime_checkout_count"] == 4
        assert data["currently_held_count"] == 2
        assert data["currently_overdue_count"] == 1
        assert data["mean_hold_duration_days"] == pytest.approx(3.0, abs=0.01)

    def test_summary_unknown_employee_returns_404(self, auth_client):
        resp = auth_client.get("/api/v1/employees/NOPE/summary/")
        assert resp.status_code == status.HTTP_404_NOT_FOUND

    def test_summary_no_returned_checkouts_gives_null_mean(
        self, auth_client, make_asset, make_employee
    ):
        employee = make_employee(code="EMP-301")
        asset = make_asset(tag="AST-304", status=Asset.Status.CHECKED_OUT)
        CheckOut.objects.create(
            asset=asset, employee=employee, due_at=timezone.now() + timedelta(days=5)
        )

        resp = auth_client.get(f"/api/v1/employees/{employee.employee_code}/summary/")
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json()["mean_hold_duration_days"] is None
