from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from core.models import Asset, CheckOut, Employee


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def auth_client(db):
    user = get_user_model().objects.create_user(username="tester", password="pw12345")
    token = Token.objects.create(user=user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return client


@pytest.fixture
def make_asset(db):
    def _make(tag="AST-100", status=Asset.Status.AVAILABLE, category=Asset.Category.CAMERA):
        return Asset.objects.create(
            asset_tag=tag,
            name=f"Test asset {tag}",
            category=category,
            status=status,
            purchase_date=timezone.now().date(),
        )

    return _make


@pytest.fixture
def make_employee(db):
    counter = {"n": 0}

    def _make(code=None, is_active=True):
        counter["n"] += 1
        code = code or f"EMP-{100 + counter['n']}"
        return Employee.objects.create(
            employee_code=code,
            full_name=f"Test Employee {code}",
            email=f"{code.lower()}@example.com",
            is_active=is_active,
        )

    return _make


@pytest.fixture
def make_checkout(db):
    def _make(asset, employee, due_in_days=5, returned=False, checked_out_days_ago=1):
        now = timezone.now()
        checkout = CheckOut.objects.create(
            asset=asset,
            employee=employee,
            due_at=now + timedelta(days=due_in_days),
        )
        updates = {"checked_out_at": now - timedelta(days=checked_out_days_ago)}
        if returned:
            updates["returned_at"] = now
        CheckOut.objects.filter(pk=checkout.pk).update(**updates)
        checkout.refresh_from_db()
        return checkout

    return _make
