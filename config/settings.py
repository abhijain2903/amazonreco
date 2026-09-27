"""Django settings for ME Vendor Hub.

All environment-specific values come from environment variables (see .env.example).
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def env(key, default=None):
    return os.environ.get(key, default)


def env_bool(key, default=False):
    v = os.environ.get(key)
    return default if v is None else v.lower() in ("1", "true", "yes", "on")


SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-only-insecure-key-change-me")
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = [h.strip() for h in env("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]
CSRF_TRUSTED_ORIGINS = [o.strip() for o in env("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()]

# Demo behaviour: when True, actions that would normally wait for SAP (delivery, billing)
# or Amazon (sold units) are simulated so every flow can be clicked through end to end.
DEMO_SIMULATIONS = env_bool("HUB_DEMO_SIMULATIONS", True)
# Dev sign-in page with a user picker. Must be False in production (use Entra ID).
DEV_LOGIN = env_bool("HUB_DEV_LOGIN", DEBUG)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "django.contrib.postgres",
    "procrastinate.contrib.django",
    "mozilla_django_oidc",
    # ME Vendor Hub modules
    "core",
    "identity",
    "rules",
    "catalog",
    "orders",
    "fulfilment",
    "billing",
    "payments",
    "promotions",
    "debitnotes",
    "claims",
    "uploads",
    "integrations",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "core.middleware.LoginRequiredMiddleware",
    "core.middleware.CommandErrorMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context.hub",
            ],
            "builtins": ["core.templatetags.hub"],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("POSTGRES_DB", "mehub"),
        "USER": env("POSTGRES_USER", "postgres"),
        "PASSWORD": env("POSTGRES_PASSWORD", ""),
        "HOST": env("POSTGRES_HOST", "localhost"),
        "PORT": env("POSTGRES_PORT", "5432"),
        "CONN_MAX_AGE": 60,
        "OPTIONS": {"sslmode": env("POSTGRES_SSLMODE", "prefer")},
    }
}

AUTH_USER_MODEL = "identity.User"
AUTHENTICATION_BACKENDS = ["django.contrib.auth.backends.ModelBackend"]
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/login/"

# Microsoft Entra ID (OIDC). Enabled when OIDC_RP_CLIENT_ID is set.
OIDC_ENABLED = bool(env("OIDC_RP_CLIENT_ID"))
if OIDC_ENABLED:
    AUTHENTICATION_BACKENDS.insert(0, "identity.oidc.EntraBackend")
    tenant = env("OIDC_TENANT_ID", "")
    OIDC_RP_CLIENT_ID = env("OIDC_RP_CLIENT_ID")
    OIDC_RP_CLIENT_SECRET = env("OIDC_RP_CLIENT_SECRET")
    OIDC_RP_SIGN_ALGO = "RS256"
    OIDC_OP_AUTHORIZATION_ENDPOINT = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize"
    OIDC_OP_TOKEN_ENDPOINT = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
    OIDC_OP_USER_ENDPOINT = "https://graph.microsoft.com/oidc/userinfo"
    OIDC_OP_JWKS_ENDPOINT = f"https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys"
    OIDC_RP_SCOPES = "openid email profile"
    OIDC_USE_PKCE = True
    # Entra group object id -> hub role, e.g. "1111-...:PIC,2222-...:Finance"
    OIDC_GROUP_ROLE_MAP = dict(
        pair.split(":", 1) for pair in env("OIDC_GROUP_ROLE_MAP", "").split(",") if ":" in pair
    )

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = 8 * 3600
SESSION_SAVE_EVERY_REQUEST = True
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
if not DEBUG:
    SECURE_SSL_REDIRECT = env_bool("DJANGO_SSL_REDIRECT", True)
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_HSTS_SECONDS = 31536000

LANGUAGE_CODE = "en-gb"
TIME_ZONE = "Asia/Riyadh"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
MEDIA_URL = "/media/"
MEDIA_ROOT = Path(env("HUB_MEDIA_ROOT", str(BASE_DIR / "media")))
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage" if not DEBUG
                    else "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
# For S3-compatible object storage in production, install django-storages and set
# STORAGES["default"]["BACKEND"] = "storages.backends.s3.S3Storage" with AWS_* variables.

FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = 26 * 1024 * 1024
HUB_MAX_UPLOAD_BYTES = 25 * 1024 * 1024
HUB_API_TOKEN = env("HUB_API_TOKEN", "")  # Bearer token for system-to-system calls (acts as HUB_API_USER)
HUB_API_USER = env("HUB_API_USER", "admin")

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

EMAIL_BACKEND = env("DJANGO_EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
DEFAULT_FROM_EMAIL = env("HUB_FROM_EMAIL", "vendorhub@example.com")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"json": {"format": '{"t":"%(asctime)s","lvl":"%(levelname)s","log":"%(name)s","msg":"%(message)s"}'}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "json"}},
    "root": {"handlers": ["console"], "level": env("HUB_LOG_LEVEL", "INFO")},
    "loggers": {"django.db.backends": {"level": "WARNING"}, "procrastinate": {"level": "WARNING"}},
}

# Static files (WhiteNoise) are served as sync file streams; harmless under ASGI.
import warnings  # noqa: E402

warnings.filterwarnings("ignore", message="StreamingHttpResponse must consume synchronous iterators")
