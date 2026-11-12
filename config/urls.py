from django.contrib.auth.decorators import login_required
from django.db import connection
from django.http import HttpResponse
from django.shortcuts import render
from django.urls import include, path


@login_required
def home(request):
    return render(request, "home.html")


def healthz(request):
    """For Uptime Kuma: 200 only if the database answers."""
    with connection.cursor() as cur:
        cur.execute("SELECT 1")
    return HttpResponse("ok", content_type="text/plain")


urlpatterns = [
    path("healthz", healthz),
    path("", home, name="home"),
    path("account/", include("accounts.urls")),
    # The Django admin is for emergencies only and is blocked at Caddy.
    # It is intentionally not mounted until it is needed.
]
