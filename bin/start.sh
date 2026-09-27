#!/bin/sh
# Container entrypoint: migrate, optionally load example data and run the job worker, then serve.
set -e
python manage.py migrate --noinput
if [ "${HUB_SEED_DEMO:-false}" = "true" ]; then
  python manage.py seed_demo            # does nothing if data is already there
fi
if [ "${HUB_RUN_WORKER:-false}" = "true" ]; then
  # Single-container demos: run the background worker next to the web server.
  python manage.py procrastinate worker &
fi
exec gunicorn config.asgi:application -k uvicorn.workers.UvicornWorker \
  -b "0.0.0.0:${PORT:-8000}" --timeout 60 --access-logfile -
