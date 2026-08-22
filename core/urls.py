from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .views import AssetViewSet, CheckOutViewSet, EmployeeSummaryView, HealthView, OverdueReportView

router = DefaultRouter()
router.register("assets", AssetViewSet, basename="asset")
router.register("checkouts", CheckOutViewSet, basename="checkout")

urlpatterns = [
    path("", include(router.urls)),
    path(
        "employees/<str:employee_code>/summary/",
        EmployeeSummaryView.as_view(),
        name="employee-summary",
    ),
    path("reports/overdue/", OverdueReportView.as_view(), name="overdue-report"),
    path("health/", HealthView.as_view(), name="health"),
    # Alternative to token auth for clients that would rather not manage a
    # long-lived static token (see README "Assumptions").
    path("auth/token/", TokenObtainPairView.as_view(), name="jwt-obtain-pair"),
    path("auth/token/refresh/", TokenRefreshView.as_view(), name="jwt-refresh"),
    # OpenAPI schema + interactive docs.
    path("schema/", SpectacularAPIView.as_view(), name="schema"),
    path("docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="docs"),
]
