from django.conf import settings
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("", include("core.urls")),
]
if settings.DJANGO_ADMIN:
    urlpatterns.insert(0, path("admin/", admin.site.urls))
if settings.OIDC_ENABLED:
    urlpatterns.insert(0, path("oidc/", include("mozilla_django_oidc.urls")))
