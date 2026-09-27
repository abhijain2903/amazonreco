from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import User


@admin.register(User)
class HubUserAdmin(UserAdmin):
    list_display = ("username", "display_name", "email", "roles", "is_active")
    fieldsets = UserAdmin.fieldsets + (("Vendor Hub", {"fields": ("display_name", "roles")}),)
