"""Background AI review after an import, so uploads stay fast and Claude only sees the records that need it."""
import logging

from procrastinate.contrib.django import app

log = logging.getLogger("hub.jobs")


@app.task(queue="rules", retry=2)
def ai_review(kind: str, source: str):
    from debitnotes.models import DebitNote
    from payments.models import Payment

    from . import ai
    from . import services as svc
    if not ai.available():
        return
    if kind == "deduction":
        p = Payment.objects.filter(payment_no=source, status="short").first()
        if p:
            svc.refresh_deduction(p, use_ai=True)
        return
    obj = (Payment.objects.filter(payment_no=source).first() if kind in ("pay_inv", "pay_dn")
           else DebitNote.objects.filter(dn_no=source).first())
    if obj is not None:
        svc.refresh(kind, obj, use_ai=True)
