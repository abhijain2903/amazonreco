from django.contrib import admin

from .models import SavedMapping, UploadBatch

admin.site.register(UploadBatch, list_display=("filename", "upload_type", "status", "rows_total", "error_count", "uploaded_by_name", "created_at"),
                    list_filter=("upload_type", "status"), exclude=("raw_rows",))
admin.site.register(SavedMapping, list_display=("upload_type", "header_signature"))
