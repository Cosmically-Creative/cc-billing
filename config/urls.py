from billing.views import dashboard
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
    path("", dashboard, name="home"),
    path("", include("billing.urls")),
    path("account/", include("accounts.urls")),
    # The Django admin is for emergencies only and is blocked at Caddy.
    # It is intentionally not mounted until it is needed.
]
