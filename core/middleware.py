import json

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
        return redirect(request.META.get("HTTP_REFERER", "/"))
