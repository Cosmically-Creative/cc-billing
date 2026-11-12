from django.contrib.auth import logout
from django.shortcuts import redirect

SESSION_VERIFIED = "ccb_2fa_verified"

# Paths reachable without a fully verified sign-in.
PUBLIC_PREFIXES = (
    "/account/login/",
    "/account/verify/",
    "/account/setup/",
    "/healthz",
    "/pay/",  # client pay page (added later)
    "/stripe/webhook",  # signature-verified (added later)
    "/static/",
)


class RequireTwoFactorMiddleware:
    """A password alone never counts as signed in: the session must also pass the authenticator step."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path
        if not path.startswith(PUBLIC_PREFIXES):
            user = getattr(request, "user", None)
            if user is not None and user.is_authenticated and not request.session.get(SESSION_VERIFIED):
                logout(request)
                return redirect("accounts:login")
        return self.get_response(request)
