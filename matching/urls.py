from django.urls import path

from . import views

urlpatterns = [
    path("<uuid:sid>/accept/", views.accept),
    path("<uuid:sid>/reject/", views.reject),
    path("review/<str:kind>/<str:source>/", views.review),
    path("settings/", views.settings_save),
]
