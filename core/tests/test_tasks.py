from datetime import timedelta

import pytest
from django.utils import timezone

from core.models import Asset, CheckOut, OverdueNotice
from core.tasks import flag_overdue_checkouts

pytestmark = pytest.mark.django_db


class TestFlagOverdueCheckoutsTask:
    def test_creates_one_notice_per_overdue_checkout(self, make_asset, make_employee):
        now = timezone.now()
        employee = make_employee(code="EMP-500")

        overdue_asset = make_asset(tag="AST-500", status=Asset.Status.CHECKED_OUT)
        overdue_checkout = CheckOut.objects.create(
            asset=overdue_asset, employee=employee, due_at=now - timedelta(days=2)
        )

        not_overdue_asset = make_asset(tag="AST-501", status=Asset.Status.CHECKED_OUT)
        CheckOut.objects.create(asset=not_overdue_asset, employee=employee, due_at=now + timedelta(days=2))

        result = flag_overdue_checkouts()

        assert result["created"] == 1
        assert OverdueNotice.objects.filter(checkout=overdue_checkout).count() == 1

    def test_running_twice_in_one_day_does_not_duplicate_notices(self, make_asset, make_employee):
        now = timezone.now()
        employee = make_employee(code="EMP-501")
        asset = make_asset(tag="AST-502", status=Asset.Status.CHECKED_OUT)
        checkout = CheckOut.objects.create(
            asset=asset, employee=employee, due_at=now - timedelta(days=1)
        )

        first = flag_overdue_checkouts()
        second = flag_overdue_checkouts()

        assert first["created"] == 1
        assert second["created"] == 0
        assert second["skipped"] == 1
        assert OverdueNotice.objects.filter(checkout=checkout).count() == 1

    def test_returned_checkouts_are_never_flagged(self, make_asset, make_employee):
        now = timezone.now()
        employee = make_employee(code="EMP-502")
        asset = make_asset(tag="AST-503")
        checkout = CheckOut.objects.create(
            asset=asset, employee=employee, due_at=now - timedelta(days=1)
        )
        CheckOut.objects.filter(pk=checkout.pk).update(returned_at=now)

        result = flag_overdue_checkouts()

        assert result["created"] == 0
        assert OverdueNotice.objects.filter(checkout=checkout).count() == 0
