from django.conf import settings
from django.db import models


class TOTPDevice(models.Model):
    """One authenticator-app secret per user. Created only after a code is confirmed."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="totp_device")
    secret = models.CharField(max_length=64)
    # Highest 30-second time step already used; a code can never be replayed.
    last_used_step = models.BigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"TOTP for {self.user}"
