from django.urls import path

from . import views

urlpatterns = [
    path("", views.pos_list),
    path("<str:po_no>/lines/", views.lines_save),
    path("<str:po_no>/accept-green/", views.accept_green),
    path("<str:po_no>/confirm/", views.confirm),
    path("<str:po_no>/book/", views.book),
    path("<str:po_no>/release/", views.release),
    path("<str:po_no>/hold/", views.hold),
    path("<str:po_no>/change/", views.change),
    path("<str:po_no>/references/", views.references),
    path("<str:po_no>/backorder/ship/", views.ship_backorder),
    path("<str:po_no>/backorder/close/", views.close_backorder),
    path("<str:po_no>/sync-delivery/", views.sync_delivery),
    path("<str:po_no>/asn/", views.submit_asn),
    path("<str:po_no>/slot/", views.slot),
    path("<str:po_no>/slot-failed/", views.slot_failed),
    path("<str:po_no>/deliver/", views.deliver),
    path("<str:po_no>/fix-billing/", views.fix_billing),
    path("<str:po_no>/invoice/", views.submit_invoice),
    path("<str:po_no>/invoice/status/", views.invoice_status),
    path("<str:po_no>/invoice/resubmit/", views.invoice_resubmit),
    path("<str:po_no>/credit-memo/", views.credit_memo),
]
