"""Authenticator-app (TOTP) helpers: RFC 6238, 30-second steps, 6 digits."""

import io
import time

import pyotp
import qrcode
import qrcode.image.svg
from django.conf import settings

STEP_SECONDS = 30


def new_secret() -> str:
    return pyotp.random_base32(length=32)


def provisioning_uri(secret: str, username: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=settings.TOTP_ISSUER)


def qr_svg(uri: str) -> str:
    img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage, box_size=8)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode()


def match_step(secret: str, code: str, now: float | None = None, window: int = 1) -> int | None:
    """Return the time step the code belongs to (allowing ±window steps of clock drift), or None."""
    code = (code or "").strip().replace(" ", "")
    if not (len(code) == 6 and code.isdigit()):
        return None
    now = time.time() if now is None else now
    totp = pyotp.TOTP(secret)
    current = int(now // STEP_SECONDS)
    for step in range(current - window, current + window + 1):
        if pyotp.utils.strings_equal(totp.at(step * STEP_SECONDS), code):
            return step
    return None


def verify_device(device, code: str, now: float | None = None) -> bool:
    """Check a code against a saved device and burn its time step so it can't be reused."""
    from .models import TOTPDevice

    step = match_step(device.secret, code, now)
    if step is None or step <= device.last_used_step:
        return False
    # Conditional update: two simultaneous requests with the same code can't both win.
    updated = TOTPDevice.objects.filter(pk=device.pk, last_used_step__lt=step).update(last_used_step=step)
    if updated:
        device.last_used_step = step
    return bool(updated)
