from django.urls import path

from . import views

urlpatterns = [
    path("", views.claim_list),
    path("<str:claim_no>/cn/", views.cn),
    path("<str:claim_no>/chase/", views.chase),
    path("<str:claim_no>/write-off/", views.write_off),
]
