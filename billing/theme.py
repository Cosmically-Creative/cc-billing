"""Owner screen appearance: Light, Dark or Match device, remembered per browser in a cookie.

The choice only changes the screens' colors. PDFs, emails and the public pay page are unaffected.
"""
from django.http import HttpResponseBadRequest
from django.shortcuts import redirect
from django.utils.http import url_has_allowed_host_and_scheme

COOKIE = 'ccb_theme'
CHOICES = (('light', 'Light'), ('dark', 'Dark'), ('system', 'Match device'))
DEFAULT = 'system'
MAX_AGE = 365 * 24 * 3600


def current(request):
    value = request.COOKIES.get(COOKIE, '')
    return value if value in dict(CHOICES) else DEFAULT


def set_theme(request):
    """POST theme=<light|dark|system>, next=<same-site path>. Works without JavaScript."""
    value = request.POST.get('theme', '')
    if value not in dict(CHOICES):
        return HttpResponseBadRequest('Unknown appearance.')
    target = request.POST.get('next', '')
    if not url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        target = '/'
    response = redirect(target)
    response.set_cookie(COOKIE, value, max_age=MAX_AGE, secure=request.is_secure(), httponly=True, samesite='Lax')
    return response
