from django.urls import path

from . import views

urlpatterns = [
    path("", views.promo_list),
    path("new/", views.wizard),
    path("budgets/", views.budget_save),
    path("<str:ref>/fee/", views.fee),
    path("<str:ref>/instalments/", views.instalments),
    path("<str:ref>/amend/", views.amend),
    path("<str:ref>/submit/", views.submit),
    path("<str:ref>/approve/", views.approve),
    path("<str:ref>/reject/", views.reject),
    path("<str:ref>/sold/", views.sold),
    path("<str:ref>/chase/", views.chase),
    path("<str:ref>/claim/", views.claim),
]
