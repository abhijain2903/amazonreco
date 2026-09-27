from django.urls import path

from . import views

urlpatterns = [
    path("", views.pay_list),
    path("automatch/", views.automatch),
    path("<str:pay_no>/match/", views.match),
    path("<str:pay_no>/dispute/", views.dispute),
    path("<str:pay_no>/accept/", views.accept),
    path("<str:pay_no>/link-dn/", views.link_dn),
    path("disputes/<str:case_no>/<str:status>/", views.dispute_status),
]
