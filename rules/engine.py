"""Rules R1-R12 as pure functions.

No Django imports here: the same functions are used by pages, background jobs and unit tests.
Money is always integer halalas (SAR x 100). Times are timezone-aware datetimes.
"""
from dataclasses import dataclass, field
from datetime import timedelta

DEFAULTS = {
    "R1": ("Price check", "PO cost matches agreed cost", {"pct": 0.5, "abs": 1}),
    "R2": ("Stock check", "PO quantity is within free stock", {}),
    "R3": ("Confirmation deadline", "Alert before the confirm-by time", {"hrs": 12}),
    "R4": ("ASN match", "ASN qty = SAP delivery = confirmed qty", {}),
    "R5": ("Slot booked", "Delivery slot exists before dispatch", {"hrs": 48}),
    "R6": ("Invoice match", "Invoice = PO price x ASN qty", {}),
    "R7": ("Payment match", "Amount paid = invoice total", {}),
    "R8": ("Unique promo", "One MECL ref links to one agreement #", {}),
    "R9": ("DN timing", "Alert when a DN is late", {"days": 15}),
    "R10": ("DN match", "DN = sold units x agreed support", {}),
    "R11": ("CN match", "Credit note = claim amount", {}),
    "R12": ("Tolerance", "Differences up to this amount auto-pass", {"abs": 1}),
}


@dataclass
class Cfg:
    """Rule settings: {rule_id: {"enabled": bool, **params}}; version = sum of rule versions."""

    rules: dict = field(default_factory=dict)
    version: int = 1

    @classmethod
    def defaults(cls):
        return cls({r: {"enabled": True, **p} for r, (_, _, p) in DEFAULTS.items()})

    def on(self, r):
        return self.rules.get(r, {}).get("enabled", True)

    def p(self, r, key, default=0):
        return self.rules.get(r, {}).get(key, DEFAULTS[r][2].get(key, default))

    def tol_h(self):
        """R12 tolerance in halalas."""
        return round(float(self.p("R12", "abs")) * 100) if self.on("R12") else 0


# ---------- sell-in ----------
def price_check(cost_h, agreed_h, cfg: Cfg):
    """R1. Returns (ok, diff_h). Passes when agreed cost is unknown or within tolerance."""
    if agreed_h is None or agreed_h == 0:
        return True, 0
    diff = cost_h - agreed_h
    limit = max(round(float(cfg.p("R1", "abs")) * 100), round(agreed_h * float(cfg.p("R1", "pct")) / 100))
    return (not cfg.on("R1")) or abs(diff) <= limit, diff


def stock_check(qty, stock, cfg: Cfg):
    """R2."""
    return (not cfg.on("R2")) or qty <= (stock or 0)


def suggest(cost_h, agreed_h, qty, stock, cfg: Cfg):
    """Suggested decision for a PO line: (decision, qty_confirmed, reason)."""
    price_ok, _ = price_check(cost_h, agreed_h, cfg)
    if not price_ok:
        return "reject", 0, "Cost differs from agreed price"
    if not stock_check(qty, stock, cfg):
        return ("partial", stock, "Limited stock") if stock and stock > 0 else ("reject", 0, "Out of stock")
    return "accept", qty, ""


def line_tone(cost_h, agreed_h, qty, stock, cfg: Cfg):
    if not price_check(cost_h, agreed_h, cfg)[0]:
        return "bad"
    if not stock_check(qty, stock, cfg):
        return "warn"
    return "ok"


def confirm_state(confirm_by, now, cfg: Cfg):
    """R3: 'overdue', 'due_soon' or 'ok'."""
    if confirm_by < now:
        return "overdue"
    if cfg.on("R3") and confirm_by - now < timedelta(hours=float(cfg.p("R3", "hrs"))):
        return "due_soon"
    return "ok"


def asn_line(confirmed, delivery, asn, cfg: Cfg):
    """R4: ASN must equal the SAP delivery (blocking); delivery short of confirmed is a warning."""
    return {"asn_ok": (not cfg.on("R4")) or asn == delivery, "del_ok": delivery == confirmed}


def slot_at_risk(has_slot, ship_date, now, cfg: Cfg):
    """R5."""
    return cfg.on("R5") and not has_slot and ship_date - now < timedelta(hours=float(cfg.p("R5", "hrs")))


def invoice_line(asn_qty, bill_qty, po_price_h, bill_price_h, cfg: Cfg):
    """R6."""
    if not cfg.on("R6"):
        return {"qty_ok": True, "price_ok": True}
    return {"qty_ok": bill_qty == asn_qty, "price_ok": abs(bill_price_h - po_price_h) <= 1}


def payment_match(paid_h, due_h, cfg: Cfg):
    """R7: 'matched' or 'short'."""
    if not cfg.on("R7"):
        return "matched"
    return "matched" if paid_h >= due_h - cfg.tol_h() else "short"


# ---------- sell-out ----------
def dn_overdue(dn_due, now, cfg: Cfg):
    """R9."""
    return cfg.on("R9") and now > dn_due + timedelta(days=float(cfg.p("R9", "days")))


def dn_check(dn_lines, promo_lines, dn_date, promo_end, cfg: Cfg):
    """R10. Compare a debit note with its promotion agreement.

    dn_lines: [{"sku": key, "units": int, "rate_h": int}]
    promo_lines: {sku key: {"support_h": int, "sold": int|None}}
    Expected = units sold (from the Amazon sales report, else the DN units) x agreed support.
    """
    out, charged, expected = [], 0, 0
    for l in dn_lines:
        pl = promo_lines.get(l["sku"])
        ch = l["units"] * l["rate_h"]
        sold = pl["sold"] if pl else None
        exp = (sold if sold is not None else l["units"]) * pl["support_h"] if pl else 0
        out.append({**l, "charged_h": ch, "expected_h": exp, "gap_h": ch - exp, "sold": sold,
                    "support_h": pl["support_h"] if pl else None, "in_promo": bool(pl),
                    "rate_ok": bool(pl) and l["rate_h"] == pl["support_h"],
                    "units_ok": bool(pl) and (sold is None or l["units"] <= sold)})
        charged += ch
        expected += exp
    variance = charged - expected
    date_ok = dn_date >= promo_end
    ok = (not cfg.on("R10")) or (abs(variance) <= cfg.tol_h() and all(x["in_promo"] and x["rate_ok"] for x in out) and date_ok)
    return {"lines": out, "charged_h": charged, "expected_h": expected, "variance_h": variance, "date_ok": date_ok, "ok": ok}


def cn_check(claim_h, cn_h, cfg: Cfg):
    """R11: 'closed' or 'shortfall'."""
    return "closed" if (not cfg.on("R11")) or abs(claim_h - cn_h) <= cfg.tol_h() else "shortfall"
