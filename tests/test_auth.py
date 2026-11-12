"""Password + authenticator sign-in."""

import time

import pyotp
import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache

from accounts import totp
from accounts.models import TOTPDevice

PASSWORD = "sample-password-for-tests-only"


@pytest.fixture(autouse=True)
def _clear_throttle():
    cache.clear()


@pytest.fixture
def owner(db):
    return get_user_model().objects.create_user("owner", password=PASSWORD)


def _password_step(client):
    return client.post("/account/login/", {"username": "owner", "password": PASSWORD})


def test_first_sign_in_sets_up_the_authenticator(client, owner):
    assert _password_step(client).url == "/account/setup/"
    page = client.get("/account/setup/")
    assert page.status_code == 200 and b"<svg" in page.content
    secret = client.session["ccb_pending_secret"]
    assert client.get("/").status_code == 302  # not signed in yet
    resp = client.post("/account/setup/", {"code": pyotp.TOTP(secret).now()})
    assert resp.url == "/"
    assert TOTPDevice.objects.filter(user=owner).exists()
    assert client.get("/").status_code == 200


def test_password_alone_is_not_enough(client, owner):
    TOTPDevice.objects.create(user=owner, secret=totp.new_secret())
    assert _password_step(client).url == "/account/verify/"
    assert client.get("/").status_code == 302


def test_code_cannot_be_reused(client, owner):
    device = TOTPDevice.objects.create(user=owner, secret=totp.new_secret())
    code = pyotp.TOTP(device.secret).now()
    _password_step(client)
    assert client.post("/account/verify/", {"code": code}).url == "/"
    client.post("/account/logout/")
    _password_step(client)
    resp = client.post("/account/verify/", {"code": code})
    assert resp.status_code == 200 and b"didn" in resp.content  # same code refused


def test_wrong_codes_and_passwords_are_throttled(client, owner):
    for _ in range(5):
        client.post("/account/login/", {"username": "owner", "password": "wrong"})
    resp = _password_step(client)
    assert resp.status_code == 200 and b"Too many attempts" in resp.content


def test_clock_drift_window(owner, db):
    device = TOTPDevice.objects.create(user=owner, secret=totp.new_secret())
    now = time.time()
    old = pyotp.TOTP(device.secret).at(now - 30)
    assert totp.verify_device(device, old, now)  # one step behind is fine
    stale = pyotp.TOTP(device.secret).at(now - 120)
    assert not totp.verify_device(device, stale, now)


def test_health_check_is_public(client, db):
    assert client.get("/healthz").content == b"ok"
