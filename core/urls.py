from django.urls import include, path

from config.api import api

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("action/", views.action, name="action"),
    path("nav/", views.nav),
    path("topbar/", views.topbar),
    path("notifications/", views.notifications),
    path("notifications/read-all/", views.notifications_read),
    path("notifications/<int:pk>/", views.notification_open),
    path("events/", views.events),
    path("search/", views.search),
    path("records/<str:kind>/<str:key>/", views.record),
    path("notes/<str:entity>/<str:key>/", views.add_note),
    path("files/<uuid:pk>/", views.file_view),
    path("login/", views.login_view),
    path("logout/", views.logout_view),
    path("dev/switch/", views.dev_switch),
    path("healthz", views.healthz),
    path("settings/", views.settings_page),
    path("settings/rules/<str:rule_id>/", views.rule_update),
    path("settings/fcs/add/", views.fc_add),
    path("pos/", include("orders.urls")),
    path("ship/", include("fulfilment.urls")),
    path("pay/", include("payments.urls")),
    path("promos/", include("promotions.urls")),
    path("dns/", include("debitnotes.urls")),
    path("claims/", include("claims.urls")),
    path("uploads/", include("uploads.urls")),
    path("integrations/", include("integrations.urls")),
    path("matching/", include("matching.urls")),
    path("api/v1/", api.urls),
]
