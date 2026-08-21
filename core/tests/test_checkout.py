import threading
from datetime import timedelta

import pytest
from django.db import connection
from django.urls import reverse
from django.utils import timezone
from rest_framework import status

from core.models import Asset, CheckOut, Employee

pytestmark = pytest.mark.django_db


def due_at(days=5):
    return (timezone.now() + timedelta(days=days)).isoformat()


class TestCheckOutCreate:
    def test_successful_checkout_sets_asset_status(self, auth_client, make_asset, make_employee):
        asset = make_asset(tag="AST-200")
        employee = make_employee(code="EMP-200")

        resp = auth_client.post(
            "/api/v1/checkouts/",
            {"asset_tag": asset.asset_tag, "employee_code": employee.employee_code, "due_at": due_at()},
            format="json",
        )

        assert resp.status_code == status.HTTP_201_CREATED
        asset.refresh_from_db()
        assert asset.status == Asset.Status.CHECKED_OUT

    def test_unavailable_asset_returns_409(self, auth_client, make_asset, make_employee):
        asset = make_asset(tag="AST-201", status=Asset.Status.CHECKED_OUT)
        employee = make_employee(code="EMP-201")

        resp = auth_client.post(
            "/api/v1/checkouts/",
            {"asset_tag": asset.asset_tag, "employee_code": employee.employee_code, "due_at": due_at()},
            format="json",
        )
        assert resp.status_code == status.HTTP_409_CONFLICT

    def test_inactive_employee_returns_400(self, auth_client, make_asset, make_employee):
        asset = make_asset(tag="AST-202")
        employee = make_employee(code="EMP-202", is_active=False)

        resp = auth_client.post(
            "/api/v1/checkouts/",
            {"asset_tag": asset.asset_tag, "employee_code": employee.employee_code, "due_at": due_at()},
            format="json",
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

    def test_due_at_in_past_returns_400(self, auth_client, make_asset, make_employee):
        asset = make_asset(tag="AST-203")
        employee = make_employee(code="EMP-203")

        resp = auth_client.post(
            "/api/v1/checkouts/",
            {
                "asset_tag": asset.asset_tag,
                "employee_code": employee.employee_code,
                "due_at": (timezone.now() - timedelta(days=1)).isoformat(),
            },
            format="json",
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

    def test_due_at_too_far_ahead_returns_400(self, auth_client, make_asset, make_employee):
        asset = make_asset(tag="AST-204")
        employee = make_employee(code="EMP-204")

        resp = auth_client.post(
            "/api/v1/checkouts/",
            {
                "asset_tag": asset.asset_tag,
                "employee_code": employee.employee_code,
                "due_at": due_at(days=31),
            },
            format="json",
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

    def test_unknown_asset_returns_404(self, auth_client, make_employee):
        employee = make_employee(code="EMP-205")
        resp = auth_client.post(
            "/api/v1/checkouts/",
            {"asset_tag": "NOPE", "employee_code": employee.employee_code, "due_at": due_at()},
            format="json",
        )
        assert resp.status_code == status.HTTP_404_NOT_FOUND

    def test_unknown_employee_returns_404(self, auth_client, make_asset):
        asset = make_asset(tag="AST-206")
        resp = auth_client.post(
            "/api/v1/checkouts/",
            {"asset_tag": asset.asset_tag, "employee_code": "NOPE", "due_at": due_at()},
            format="json",
        )
        assert resp.status_code == status.HTTP_404_NOT_FOUND

    def test_fourth_open_checkout_returns_409(self, auth_client, make_asset, make_employee):
        employee = make_employee(code="EMP-207")
        for i in range(3):
            asset = make_asset(tag=f"AST-207-{i}")
            resp = auth_client.post(
                "/api/v1/checkouts/",
                {"asset_tag": asset.asset_tag, "employee_code": employee.employee_code, "due_at": due_at()},
                format="json",
            )
            assert resp.status_code == status.HTTP_201_CREATED

        fourth_asset = make_asset(tag="AST-207-x")
        resp = auth_client.post(
            "/api/v1/checkouts/",
            {"asset_tag": fourth_asset.asset_tag, "employee_code": employee.employee_code, "due_at": due_at()},
            format="json",
        )
        assert resp.status_code == status.HTTP_409_CONFLICT


class TestCheckOutReturn:
    def test_return_sets_asset_available(self, auth_client, make_asset, make_employee, make_checkout):
        asset = make_asset(tag="AST-210", status=Asset.Status.CHECKED_OUT)
        employee = make_employee(code="EMP-210")
        checkout = make_checkout(asset, employee)

        resp = auth_client.post(
            f"/api/v1/checkouts/{checkout.id}/return/",
            {"condition_note": "fine", "needs_maintenance": False},
            format="json",
        )
        assert resp.status_code == status.HTTP_200_OK
        asset.refresh_from_db()
        assert asset.status == Asset.Status.AVAILABLE

    def test_return_with_maintenance_flag(self, auth_client, make_asset, make_employee, make_checkout):
        asset = make_asset(tag="AST-211", status=Asset.Status.CHECKED_OUT)
        employee = make_employee(code="EMP-211")
        checkout = make_checkout(asset, employee)

        resp = auth_client.post(
            f"/api/v1/checkouts/{checkout.id}/return/",
            {"condition_note": "cracked lens", "needs_maintenance": True},
            format="json",
        )
        assert resp.status_code == status.HTTP_200_OK
        asset.refresh_from_db()
        assert asset.status == Asset.Status.MAINTENANCE

    def test_double_return_returns_409(self, auth_client, make_asset, make_employee, make_checkout):
        asset = make_asset(tag="AST-212", status=Asset.Status.CHECKED_OUT)
        employee = make_employee(code="EMP-212")
        checkout = make_checkout(asset, employee)

        first = auth_client.post(
            f"/api/v1/checkouts/{checkout.id}/return/",
            {"condition_note": "", "needs_maintenance": False},
            format="json",
        )
        assert first.status_code == status.HTTP_200_OK

        second = auth_client.post(
            f"/api/v1/checkouts/{checkout.id}/return/",
            {"condition_note": "", "needs_maintenance": False},
            format="json",
        )
        assert second.status_code == status.HTTP_409_CONFLICT


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason=(
        "SQLite's backend does not implement SELECT ... FOR UPDATE (Django "
        "silently no-ops it), so it cannot demonstrate real row-level "
        "locking. Run this test against the Postgres service, e.g. via "
        "docker-compose, for it to be meaningful."
    ),
)
class TestCheckOutConcurrency:
    def test_only_one_of_two_simultaneous_checkouts_succeeds(self, make_asset, make_employee):
        from django.contrib.auth import get_user_model
        from rest_framework.authtoken.models import Token
        from rest_framework.test import APIClient

        asset = make_asset(tag="AST-CONC")
        employee_a = make_employee(code="EMP-CONC-A")
        employee_b = make_employee(code="EMP-CONC-B")

        user = get_user_model().objects.create_user(username="conc", password="pw12345")
        token = Token.objects.create(user=user)

        results = {}

        def attempt(key, employee_code):
            client = APIClient()
            client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
            resp = client.post(
                "/api/v1/checkouts/",
                {"asset_tag": asset.asset_tag, "employee_code": employee_code, "due_at": due_at()},
                format="json",
            )
            results[key] = resp.status_code
            connection.close()  # each thread needs its own DB connection

        t1 = threading.Thread(target=attempt, args=("a", employee_a.employee_code))
        t2 = threading.Thread(target=attempt, args=("b", employee_b.employee_code))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        statuses = sorted(results.values())
        assert statuses == [status.HTTP_201_CREATED, status.HTTP_409_CONFLICT]
        assert CheckOut.objects.filter(asset=asset, returned_at__isnull=True).count() == 1
