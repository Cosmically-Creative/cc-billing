from billing.views import dashboard
from billing import private_api, public_views
from django.db import connection
from django.http import HttpResponse
from django.urls import include, path


def healthz(request):
    """For Uptime Kuma: 200 only if the database answers."""
    with connection.cursor() as cur:
        cur.execute("SELECT 1")
    return HttpResponse("ok", content_type="text/plain")


urlpatterns = [
    path("healthz", healthz),
    path("pay/<str:code>", public_views.pay, name="pay_invoice"),
    path("pay/<str:code>/pdf", public_views.invoice_pdf, name="public_pdf"),
    path("pay/<str:code>/close", public_views.close_checkout, name="close_checkout"),
    path("stripe/webhook", public_views.webhook),
    path("api/v1/call/", private_api.api_call),
    path("mcp", private_api.mcp),
    path("", dashboard, name="home"),
    path("", include("billing.urls")),
    path("account/", include("accounts.urls")),
    # The Django admin is for emergencies only and is blocked at Caddy.
    # It is intentionally not mounted until it is needed.
]
