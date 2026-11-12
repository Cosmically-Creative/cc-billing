#!/bin/sh
set -e
# Apply database migrations (including the lock and history guards) before serving.
python manage.py migrate --noinput
exec "$@"
