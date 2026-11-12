import time

from django.conf import settings
from django.contrib.auth import authenticate, get_user_model, login, logout
from django.core.cache import cache
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from . import totp
from .middleware import SESSION_VERIFIED
from .models import TOTPDevice

PENDING_USER = "ccb_pending_user"
PENDING_AT = "ccb_pending_at"
PENDING_SECRET = "ccb_pending_secret"
PENDING_MAX_AGE = 5 * 60


def _client_ip(request):
    # Caddy sets X-Forwarded-For; only the first hop matters and we never trust it for auth, only for throttling.
    fwd = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return fwd.split(",")[0].strip() or request.META.get("REMOTE_ADDR", "")


def _throttle_key(request, username):
    return f"login-fail:{username.lower()}:{_client_ip(request)}"


def _too_many(request, username):
    return cache.get(_throttle_key(request, username), 0) >= settings.LOGIN_MAX_ATTEMPTS


def _fail(request, username):
    key = _throttle_key(request, username)
    cache.set(key, cache.get(key, 0) + 1, settings.LOGIN_LOCKOUT_SECONDS)


def _pending_user(request):
    uid, at = request.session.get(PENDING_USER), request.session.get(PENDING_AT, 0)
    if not uid or time.time() - at > PENDING_MAX_AGE:
        return None
    return get_user_model().objects.filter(pk=uid, is_active=True).first()


def _finish_login(request, user):
    for key in (PENDING_USER, PENDING_AT, PENDING_SECRET):
        request.session.pop(key, None)
    login(request, user)  # rotates the session key
    request.session[SESSION_VERIFIED] = True
    return redirect(settings.LOGIN_REDIRECT_URL)


@require_http_methods(["GET", "POST"])
def login_view(request):
    error = None
    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        if _too_many(request, username):
            error = "Too many attempts. Try again in 15 minutes."
        else:
            user = authenticate(request, username=username, password=request.POST.get("password", ""))
            if user is None:
                _fail(request, username)
                error = "That username and password don't match."
            else:
                request.session.cycle_key()
                request.session[PENDING_USER] = user.pk
                request.session[PENDING_AT] = time.time()
                if TOTPDevice.objects.filter(user=user).exists():
                    return redirect("accounts:verify")
                return redirect("accounts:setup")
    return render(request, "accounts/login.html", {"error": error})


@require_http_methods(["GET", "POST"])
def verify_view(request):
    user = _pending_user(request)
    if user is None:
        return redirect("accounts:login")
    device = TOTPDevice.objects.filter(user=user).first()
    if device is None:
        return redirect("accounts:setup")
    error = None
    if request.method == "POST":
        if _too_many(request, user.get_username()):
            error = "Too many attempts. Try again in 15 minutes."
        elif totp.verify_device(device, request.POST.get("code", "")):
            return _finish_login(request, user)
        else:
            _fail(request, user.get_username())
            error = "That code didn't work. Check your authenticator app and try again."
    return render(request, "accounts/verify.html", {"error": error})


@require_http_methods(["GET", "POST"])
def setup_view(request):
    """First sign-in only: show a QR code, and save the device once a code from it is confirmed."""
    user = _pending_user(request)
    if user is None:
        return redirect("accounts:login")
    if TOTPDevice.objects.filter(user=user).exists():
        return redirect("accounts:verify")
    secret = request.session.get(PENDING_SECRET)
    if not secret:
        secret = totp.new_secret()
        request.session[PENDING_SECRET] = secret
    error = None
    if request.method == "POST":
        step = totp.match_step(secret, request.POST.get("code", ""))
        if step is not None:
            TOTPDevice.objects.create(user=user, secret=secret, last_used_step=step)
            return _finish_login(request, user)
        _fail(request, user.get_username())
        error = "That code didn't work. Scan the QR code again and enter the current code."
    uri = totp.provisioning_uri(secret, user.get_username())
    return render(
        request,
        "accounts/setup.html",
        {"error": error, "qr_svg": totp.qr_svg(uri), "secret": secret},
    )


@require_POST
def logout_view(request):
    logout(request)
    return redirect("accounts:login")
