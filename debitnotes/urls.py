from django.urls import path

from . import views

urlpatterns = [
    path("", views.dn_list),
    path("<str:dn_no>/approve/", views.approve),
    path("<str:dn_no>/override/", views.override),
    path("<str:dn_no>/dispute/", views.dispute),
    path("<str:dn_no>/link/", views.link),
]
