"""The app serves its own logo, Rubik fonts and editor script in production (DEBUG off), without sign-in."""
import pytest
from django.core.management import call_command
from django.test import Client


@pytest.mark.parametrize('path, content_type', [
    ('/static/billing/cc-logo-v11.svg', 'svg'),
    ('/static/billing/cc-logo-v10.png', 'png'),
    ('/static/billing/fonts/rubik-latin-400-normal.woff', 'woff'),
    ('/static/billing/editor.js', 'javascript'),
])
def test_static_served_without_debug(settings, tmp_path, path, content_type):
    settings.DEBUG = False
    settings.STATIC_ROOT = tmp_path
    call_command('collectstatic', interactive=False, verbosity=0)
    response = Client().get(path)  # new client → middleware reads the fresh STATIC_ROOT
    assert response.status_code == 200
    assert content_type in response['Content-Type']
    assert 'max-age' in response['Cache-Control']
