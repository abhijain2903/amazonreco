FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 DJANGO_DEBUG=false WEB_CONCURRENCY=4
WORKDIR /app

RUN useradd --create-home --uid 1000 hub
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN DJANGO_SECRET_KEY=build python manage.py collectstatic --noinput \
    && mkdir -p /data/media && chown -R hub:hub /data /app
USER hub

EXPOSE 8000
# ASGI (gunicorn + uvicorn workers) so the live-update stream (/events/) does not hold a thread per open tab.
CMD ["sh", "bin/start.sh"]
