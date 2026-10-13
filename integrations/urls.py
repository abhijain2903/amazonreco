from django.urls import path

from . import views

urlpatterns = [
    path("", views.page),
    path("<str:key>/save/", views.save),
    path("<str:key>/test/", views.test),
    path("<str:key>/sync/", views.sync),
    path("amazon/tx/<uuid:pk>/resend/", views.amazon_resend),
]
