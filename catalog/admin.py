from django.contrib import admin

from .models import FulfilmentCentre, Price, Sku

admin.site.register(FulfilmentCentre, list_display=("code", "name", "city"))
admin.site.register(Sku, list_display=("sku_code", "model_no", "asin", "category", "cost_h", "free_stock"), search_fields=("sku_code", "model_no", "asin"),
                    list_filter=("category",))
admin.site.register(Price, list_display=("sku", "cost_h", "valid_from", "valid_to"), search_fields=("sku__sku_code",))
