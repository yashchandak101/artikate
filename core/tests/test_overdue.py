from datetime import timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework import status

from core.models import Asset, CheckOut

pytestmark = pytest.mark.django_db


class TestOverdueReport:
    def test_lists_only_open_overdue_items_most_overdue_first(
        self, auth_client, make_asset, make_employee
    ):
        now = timezone.now()
        employee = make_employee(code="EMP-400")

        # Overdue by 5 days
        a1 = make_asset(tag="AST-400", status=Asset.Status.CHECKED_OUT)
        c1 = CheckOut.objects.create(asset=a1, employee=employee, due_at=now - timedelta(days=5))
        CheckOut.objects.filter(pk=c1.pk).update(checked_out_at=now - timedelta(days=20))

        # Overdue by 1 day
        a2 = make_asset(tag="AST-401", status=Asset.Status.CHECKED_OUT)
        c2 = CheckOut.objects.create(asset=a2, employee=employee, due_at=now - timedelta(days=1))
        CheckOut.objects.filter(pk=c2.pk).update(checked_out_at=now - timedelta(days=10))

        # Due a few minutes from now: not yet overdue (rule is strictly
        # past due, "due_at__lt=now"). We deliberately avoid asserting on
        # the exact-instant boundary here since that's a wall-clock race
        # in any real test run, not a meaningful assertion without mocking
        # the clock.
        a3 = make_asset(tag="AST-402", status=Asset.Status.CHECKED_OUT)
        CheckOut.objects.create(asset=a3, employee=employee, due_at=now + timedelta(minutes=5))

        # Not overdue: due in the future
        a4 = make_asset(tag="AST-403", status=Asset.Status.CHECKED_OUT)
        CheckOut.objects.create(asset=a4, employee=employee, due_at=now + timedelta(days=5))

        # Returned overdue item: must not appear even though due_at is past
        a5 = make_asset(tag="AST-404")
        c5 = CheckOut.objects.create(asset=a5, employee=employee, due_at=now - timedelta(days=3))
        CheckOut.objects.filter(pk=c5.pk).update(returned_at=now - timedelta(days=1))

        resp = auth_client.get("/api/v1/reports/overdue/")
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()

        tags_in_order = [row["asset_tag"] for row in data["results"]]
        assert tags_in_order == ["AST-400", "AST-401"]  # most overdue (5 days) first

    def test_report_does_not_issue_a_query_per_row(self, auth_client, make_asset, make_employee):
        now = timezone.now()
        for i in range(10):
            employee = make_employee(code=f"EMP-40{i}")
            asset = make_asset(tag=f"AST-4{i}0", status=Asset.Status.CHECKED_OUT)
            c = CheckOut.objects.create(
                asset=asset, employee=employee, due_at=now - timedelta(days=1)
            )
            CheckOut.objects.filter(pk=c.pk).update(checked_out_at=now - timedelta(days=5))

        with CaptureQueriesContext(connection) as ctx:
            resp = auth_client.get("/api/v1/reports/overdue/")
        assert resp.status_code == status.HTTP_200_OK
        # One query for the checkouts (+ joins via select_related), not one
        # per row. A handful of extra queries for auth/session is fine; the
        # key assertion is that it does NOT scale with the 10 rows fetched.
        assert len(ctx.captured_queries) < 5
