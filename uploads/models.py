from django.conf import settings
from django.db import models

from core.models import Base


class UploadBatch(Base):
    STATUS = [("mapping", "Mapping"), ("preview", "Preview"), ("committed", "Imported"), ("failed", "Failed")]
    upload_type = models.CharField(max_length=4)
    filename = models.CharField(max_length=200)
    file = models.FileField(upload_to="uploads/%Y/%m/", null=True, blank=True)
    checksum = models.CharField(max_length=64, blank=True)
    headers = models.JSONField(default=list)
    raw_rows = models.JSONField(default=list)
    mapping = models.JSONField(default=dict)
    status = models.CharField(max_length=10, choices=STATUS, default="mapping")
    rows_total = models.IntegerField(default=0)
    created_count = models.IntegerField(default=0)
    updated_count = models.IntegerField(default=0)
    error_count = models.IntegerField(default=0)
    warning_count = models.IntegerField(default=0)
    summary = models.JSONField(default=list)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    uploaded_by_name = models.CharField(max_length=120, blank=True)

    class Meta:
        ordering = ["-created_at"]


class UploadRow(models.Model):
    batch = models.ForeignKey(UploadBatch, on_delete=models.CASCADE, related_name="rows")
    row_no = models.IntegerField()
    data = models.JSONField()
    errors = models.JSONField(default=list)
    warnings = models.JSONField(default=list)

    class Meta:
        ordering = ["row_no"]


class SavedMapping(models.Model):
    upload_type = models.CharField(max_length=4)
    header_signature = models.CharField(max_length=64)
    mapping = models.JSONField()

    class Meta:
        unique_together = [("upload_type", "header_signature")]
