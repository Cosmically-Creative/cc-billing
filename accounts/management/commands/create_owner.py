"""Create Blake's owner account. The password is typed at the prompt, never passed as an argument."""

import getpass

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Create the owner account (authenticator setup happens at first sign-in)."

    def add_arguments(self, parser):
        parser.add_argument("username")

    def handle(self, username, **options):
        User = get_user_model()
        if User.objects.filter(username=username).exists():
            raise CommandError("That user already exists.")
        password = getpass.getpass("Password: ")
        if password != getpass.getpass("Password again: "):
            raise CommandError("Passwords don't match.")
        user = User(username=username, is_staff=True, is_superuser=True)
        validate_password(password, user)
        user.set_password(password)
        user.save()
        self.stdout.write(self.style.SUCCESS(f"Created {username}. Sign in to set up the authenticator app."))
