from django.urls import path

from . import views

urlpatterns = [
    path("", views.upload_page),
    path("new/", views.new),
    path("template/<str:tid>/", views.template),
    path("template/<str:tid>.csv", views.sample_file),
    path("<uuid:pk>/", views.step),
    path("<uuid:pk>/map/", views.mapping),
    path("<uuid:pk>/preview/", views.preview),
    path("<uuid:pk>/commit/", views.commit),
]
