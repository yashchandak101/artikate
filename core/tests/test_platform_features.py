from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db


def due_at(days=5):
    return (timezone.now() + timedelta(days=days)).isoformat()


class TestThrottling:
    def test_checkout_create_is_throttled_past_its_scoped_rate(self, auth_client):
        # Exercises the real, production-configured rate (20/minute, see
        # settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["checkout-create"])
        # rather than mutating settings at runtime, which avoids fragile
        # interaction with DRF's settings-reload signal handling under
        # pytest-django's transaction/rollback test isolation.
        cache.clear()
        payload = {"asset_tag": "NOPE", "employee_code": "NOPE", "due_at": due_at()}

        responses = [
            auth_client.post("/api/v1/checkouts/", payload, format="json").status_code
            for _ in range(22)
        ]

        # First 20 requests reach business logic (404, unknown asset/employee);
        # the 21st and 22nd are stopped by the throttle before that.
        assert responses[:20] == [status.HTTP_404_NOT_FOUND] * 20
        assert responses[20:] == [status.HTTP_429_TOO_MANY_REQUESTS] * 2
        cache.clear()

    def test_other_checkout_actions_are_not_subject_to_the_create_scope(
        self, auth_client, make_asset, make_employee
    ):
        # Sanity check that get_throttles() only narrows the create action;
        # list/retrieve keep the (much higher) default user rate.
        cache.clear()
        make_asset(tag="AST-900")
        for _ in range(5):
            resp = auth_client.get("/api/v1/checkouts/")
            assert resp.status_code == status.HTTP_200_OK
        cache.clear()


class TestJWTAuth:
    def test_obtain_and_use_jwt_access_token(self, db, make_asset):
        make_asset(tag="AST-910")
        get_user_model().objects.create_user(username="jwtuser", password="pw12345")

        client = APIClient()
        token_resp = client.post(
            "/api/v1/auth/token/",
            {"username": "jwtuser", "password": "pw12345"},
            format="json",
        )
        assert token_resp.status_code == status.HTTP_200_OK
        access = token_resp.json()["access"]
        assert "refresh" in token_resp.json()

        authed_client = APIClient()
        authed_client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        resp = authed_client.get("/api/v1/assets/")
        assert resp.status_code == status.HTTP_200_OK

    def test_bad_credentials_rejected(self, db):
        client = APIClient()
        resp = client.post(
            "/api/v1/auth/token/",
            {"username": "nobody", "password": "wrong"},
            format="json",
        )
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED


class TestOpenAPISchema:
    def test_schema_endpoint_returns_valid_openapi_document(self, api_client, db):
        resp = api_client.get("/api/v1/schema/")
        assert resp.status_code == status.HTTP_200_OK
        body = resp.content.decode()
        assert "openapi:" in body
        assert "/api/v1/checkouts/" in body
        assert "/api/v1/reports/overdue/" in body

    def test_swagger_docs_page_renders(self, api_client, db):
        resp = api_client.get("/api/v1/docs/")
        assert resp.status_code == status.HTTP_200_OK
