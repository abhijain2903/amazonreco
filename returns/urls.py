from django.urls import path

from . import views

urlpatterns = [
    path("", views.rtv_list),
    path("new/", views.new),
    path("<str:no>/authorise/", views.authorise),
    path("<str:no>/refuse/", views.refuse),
    path("<str:no>/receive/", views.receive),
    path("<str:no>/link/", views.link),
]
