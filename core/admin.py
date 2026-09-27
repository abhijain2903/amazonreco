from django.contrib import admin

from .models import AuditEvent, Counter, GeneratedFile, Note, Notification


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    """Read-only: the audit trail is append-only (enforced in the database too)."""
    list_display = ("at", "entity", "entity_id", "actor_name", "action", "text")
    list_filter = ("entity", "action")
    search_fields = ("entity_id", "text", "actor_name")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


admin.site.register(Note, list_display=("entity", "entity_id", "author_name", "created_at"))
admin.site.register(Notification, list_display=("text", "tone", "read", "at"))
admin.site.register(Counter, list_display=("key", "value"))
admin.site.register(GeneratedFile, list_display=("filename", "kind", "entity", "entity_id", "created_at"))
