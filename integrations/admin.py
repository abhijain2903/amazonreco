from django.contrib import admin

from .models import Connector, SyncRun

admin.site.register(Connector, list_display=("key", "name", "mode", "last_sync", "records_today"))
admin.site.register(SyncRun, list_display=("connector", "kind", "ok", "message", "finished_at"), list_filter=("connector", "ok"))
