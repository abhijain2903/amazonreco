from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_POST

from core import htmx
from core.services import CommandError, require

from . import ai
from . import services as svc
from .models import MatchSuggestion


def suggestions_for(kind, obj):
    """Pending suggestions for a drawer; computed (rules only) the first time a record is opened."""
    source = obj.payment_no if kind in ("pay_inv", "pay_dn") else obj.dn_no
    rows = svc.pending(kind, source)
    if not rows and not MatchSuggestion.objects.filter(kind=kind, source=source, status__in=["accepted", "auto"]).exists():
        rows = svc.refresh(kind, obj)
    return rows


def deduction_for(p):
    rows = svc.pending("deduction", p.payment_no)
    return rows[0] if rows else svc.refresh_deduction(p)


@require_POST
def accept(request, sid):
    s = svc.accept(request.user, sid, request.POST.get("version"))
    if s.kind == "pay_inv":
        from payments.models import Payment
        po = Payment.objects.get(payment_no=s.source).po
        msg = f"Payment {s.source} matched to {' + '.join(s.targets)}"
        return htmx.done(request, msg, open_drawer=f"/records/po/{po.po_no}/?tab=invoice", drawer=False)
    if s.kind == "dn_promo":
        return htmx.done(request, f"DN {s.source} linked to {s.targets[0]}", open_drawer=f"/records/promo/{s.targets[0]}/?tab=dn", drawer=False)
    return htmx.done(request, f"Deduction on {s.source} linked to {s.targets[0]}")


@require_POST
def reject(request, sid):
    s = svc.reject(request.user, sid, request.POST.get("version"))
    return htmx.done(request, f"Suggestion rejected. {s.label.split(' · ')[0]} will not be proposed again for {s.source}", "info")


@require_POST
def review(request, kind, source):
    """Recompute suggestions for one record; with ai=1 Claude reviews close calls."""
    if kind not in svc.PERM:
        raise CommandError("Unknown kind of match.")
    require(request.user, svc.PERM[kind])
    use_ai = request.POST.get("ai") == "1"
    if use_ai and not ai.available():
        raise CommandError("AI assistance is not switched on for this hub. Ask an admin (Settings → Matching).")
    if kind == "deduction":
        from payments.models import Payment
        s = svc.refresh_deduction(get_object_or_404(Payment, payment_no=source), use_ai=use_ai)
        return htmx.done(request, ("Claude: " + s.ai_rationale) if s.method == "ai" else "Suggestion refreshed", "info")
    obj = _source_obj(kind, source)
    rows = svc.refresh(kind, obj, use_ai=use_ai)
    picked = next((r for r in rows if r.method == "ai"), None)
    if use_ai:
        msg = (f"Claude suggests {picked.label.split(' · ')[0]}: {picked.ai_rationale}" if picked else
               (rows[0].extra.get("ai_note") or "Claude found no convincing match.") if rows else "No candidates to review.")
        return htmx.done(request, msg, "info")
    return htmx.done(request, f"{len(rows)} suggestion{'s' if len(rows) != 1 else ''} found" if rows else "No likely match found", "info")


def _source_obj(kind, source):
    if kind in ("pay_inv", "pay_dn"):
        from payments.models import Payment
        return get_object_or_404(Payment, payment_no=source)
    from debitnotes.models import DebitNote
    return get_object_or_404(DebitNote, dn_no=source)


@require_POST
def settings_save(request):
    f = request.POST.get("field")
    v = request.POST.get("value")
    if f in ("auto_apply", "ai_enabled"):
        svc.update_settings(request.user, **{f: v in ("on", "true", "1")})
    elif f in ("auto_threshold", "show_threshold"):
        svc.update_settings(request.user, **{f: v or 0})
    else:
        raise CommandError("Unknown setting.")
    return htmx.done(request, "Matching settings saved", refresh=False, drawer=False)
