from django.contrib import admin

from .models import RuleConfig

admin.site.register(RuleConfig, list_display=("rule_id", "name", "enabled", "params", "version"))
