from django.conf import settings
from django.urls import resolve

from identity.models import ROLE_TITLES
from identity.permissions import caps

from .services import unread_alerts


def hub(request):
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {"dev_login": settings.DEV_LOGIN}
    try:
        section = resolve(request.path).kwargs.get("section") or getattr(resolve(request.path).func, "section", "")
    except Exception:
        section = ""
    return {
        "caps": caps(user),
        "section": section,
        "unread": unread_alerts(user).count(),
        "dev_login": settings.DEV_LOGIN,
        "demo": settings.DEMO_SIMULATIONS,
        "admin_console": settings.DJANGO_ADMIN and user.is_staff,
        "role_titles": ROLE_TITLES,
        "switch_users": _switch_users() if settings.DEV_LOGIN else [],
    }


def _switch_users():
    from identity.models import User
    return list(User.objects.filter(is_active=True).order_by("date_joined"))
