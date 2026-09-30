import json
from urllib.parse import urlparse

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import redirect

from .services import CommandError


class LoginRequiredMiddleware:
    """Everything needs sign-in except the login page, OIDC callbacks, static files and health check."""

    OPEN = ("/login/", "/oidc/", "/api/", "/static/", "/healthz", "/admin/login/", "/favicon.ico")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.user.is_authenticated and not request.path.startswith(self.OPEN):
            if request.headers.get("HX-Request"):
                r = HttpResponse(status=204)
                r["HX-Redirect"] = "/login/"
                return r
            return redirect(f"/login/?next={request.path}")
        return self.get_response(request)


class SecurityHeadersMiddleware:
    """Content-Security-Policy and Permissions-Policy on every response (the other security headers come from Django).

    Scripts are self-hosted files only, with no eval: the templates use no Alpine expressions, htmx trigger filters,
    hx-on or js: values. Adding any of those would need 'unsafe-eval'. Inline styles are allowed for the style=""
    attributes in the templates. Fonts come from Google Fonts. Exempt: the Django admin (own
    inline scripts; switched off on client-facing hosts via HUB_DJANGO_ADMIN) and /api/ (JSON, plus the Swagger docs
    page that Django Ninja loads from a CDN).
    """

    EXEMPT = ("/admin/", "/api/")

    CSP = "; ".join([
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
        "font-src 'self' https://fonts.gstatic.com",
        "img-src 'self' data:",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ])
    PERMISSIONS = "camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if not request.path.startswith(self.EXEMPT):
            response.setdefault("Content-Security-Policy", self.CSP)
        response.setdefault("Permissions-Policy", self.PERMISSIONS)
        return response


def toast_header(msg, tone="ok", **extra):
    return json.dumps({"toast": {"msg": msg, "tone": tone}, **extra})


class CommandErrorMiddleware:
    """Business-rule errors become a toast (HTMX) or a flash message (full page)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exc):
        if not isinstance(exc, CommandError):
            return None
        if request.headers.get("HX-Request"):
            r = HttpResponse(status=200)
            r["HX-Reswap"] = "none"
            r["HX-Trigger"] = toast_header(str(exc), "bad")
            return r
        messages.error(request, str(exc))
        back = request.META.get("HTTP_REFERER", "/")
        # A page the user may not open must not redirect back to itself.
        if urlparse(back).path == request.path:
            back = "/"
        return redirect(back)
