"""Load the example data used in the prototype: 350 SKUs, ~46 POs at every stage, payments, disputes,
~30 promotions, debit notes and claims. Deterministic, so demos and tests always tell the same story.

    python manage.py seed_demo --reset
"""
import math
import random
from datetime import timedelta

from django.apps import apps
from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.utils import timezone

from catalog.models import FulfilmentCentre, Price, Sku
from core.models import Notification
from core.services import next_number, notify
from identity.models import User
from integrations.connectors import ensure_connectors
from rules.services import ensure_rules

CATS = {
    "PA": (110, [("WH", "Wireless headphones", 350, 1400), ("TW", "True wireless earbuds", 250, 1100), ("SP", "Portable speaker", 180, 1600),
                 ("NB", "Neckband earphones", 120, 450), ("EX", "Wired earphones", 60, 300), ("PS", "Party speaker", 900, 3200)]),
    "DI": (90, [("MC", "Mirrorless camera body", 3800, 9800), ("CC", "Compact camera", 1400, 4200), ("VC", "Vlog camera", 2200, 3900),
                ("LN", "Camera lens", 900, 7800), ("AC", "Action camera", 900, 1900), ("GK", "Shooting grip kit", 250, 700)]),
    "TV": (50, [("43X", "43-inch 4K TV", 1500, 2400), ("50X", "50-inch 4K TV", 1900, 3100), ("55X", "55-inch 4K TV", 2300, 4800),
                ("65X", "65-inch 4K TV", 3400, 7800), ("75X", "75-inch 4K TV", 5200, 11500), ("85X", "85-inch 4K TV", 7900, 14500)]),
    "HAV": (60, [("SB", "Soundbar", 650, 4200), ("HT", "Home theatre system", 1500, 5500), ("AV", "AV receiver", 1400, 4600),
                 ("SW", "Subwoofer", 600, 2400), ("BD", "Blu-ray player", 450, 1200), ("RS", "Rear speaker kit", 700, 1800)]),
    "Bundle": (40, [("KIT", "Camera + lens kit", 4500, 12500), ("TVS", "TV + soundbar bundle", 3200, 9800), ("VLK", "Vlog starter kit", 2600, 4600),
                    ("HPC", "Headphones + case bundle", 450, 1600)]),
}
FCS = [("RUH-FC1", "Riyadh FC 1", "Riyadh"), ("RUH-FC2", "Riyadh FC 2", "Riyadh"), ("JED-FC1", "Jeddah FC 1", "Jeddah"), ("DMM-FC1", "Dammam FC 1", "Dammam")]
USERS = [("faisal", "Faisal Al-Harbi", "PIC"), ("noura", "Noura Al-Otaibi", "Planning"), ("omar", "Omar Siddiqui", "Credit"),
         ("khalid", "Khalid Mansour", "Logistics"), ("reem", "Reem Al-Dosari", "Product"), ("priya", "Priya Nair", "Finance"),
         ("tariq", "Tariq Hassan", "Manager"), ("admin", "System admin", "Admin")]
OCC = ["National Day deals", "Payday deals", "Back to school", "Summer sale", "Mega deals week", "Weekend flash deals", "Brand week",
       "White Friday", "Year-end deals", "Winter sale", "New launch offer"]
CAT_CYCLE = ["DI", "PA", "DI", "PA", "DI", "TV", "DI", "PA", "HAV", "DI", "PA", "Bundle", "DI"]
ALNUM = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class Command(BaseCommand):
    help = "Load deterministic example data (the prototype's story)."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="Empty all hub tables first")

    def handle(self, *args, reset=False, **kw):
        if reset:
            self.reset()
        if Sku.objects.exists():
            self.stdout.write("Data already present. Use --reset to reload.")
            return
        self.r = random.Random(20260926)
        self.now = timezone.now().replace(second=0, microsecond=0)
        with transaction.atomic():
            ensure_rules()
            ensure_connectors()
            self.users()
            self.catalog()
            self.pos()
            self.payments()
            self.promos()
            self.notices()
        self.stdout.write(self.style.SUCCESS("Example data loaded."))

    # ---------- helpers ----------
    def reset(self):
        labels = ["core", "rules", "catalog", "orders", "fulfilment", "billing", "payments", "promotions", "debitnotes", "claims",
                  "uploads", "integrations"]
        tables = [m._meta.db_table for l in labels for m in apps.get_app_config(l).get_models()]
        tables += [User._meta.db_table, User.groups.through._meta.db_table, User.user_permissions.through._meta.db_table]
        with connection.cursor() as c:
            c.execute("TRUNCATE " + ", ".join(f'"{t}"' for t in tables) + " RESTART IDENTITY CASCADE")
        self.stdout.write("Tables emptied.")

    def t(self, d, h=0):
        return self.now + timedelta(days=d, hours=h)

    def ri(self, a, b):
        return self.r.randint(a, b)

    def code(self, n):
        return "".join(self.r.choice(ALNUM) for _ in range(n))

    def qty_for(self, cat):
        return {"PA": self.ri(20, 120), "DI": self.ri(6, 40), "TV": self.ri(4, 24), "HAV": self.ri(8, 40), "Bundle": self.ri(4, 20)}[cat]

    # ---------- people & catalog ----------
    def users(self):
        for username, name, role in USERS:
            u = User.objects.create_user(username, f"{username}@example.com", "demo", display_name=name, roles=[role],
                                         is_staff=role == "Admin", is_superuser=role == "Admin")
        self.names = {role: name for _, name, role in USERS}

    def catalog(self):
        self.fcs = [FulfilmentCentre.objects.create(code=c, name=n, city=ci) for c, n, ci in FCS]
        used, i, skus, prices = set(), 0, [], []
        for cat, (n, tys) in CATS.items():
            for k in range(n):
                code_, desc, lo, hi = tys[k % len(tys)]
                while True:
                    model = (f"TV-{code_}{self.ri(70, 95)}{self.r.choice('KLMN')}" if cat == "TV"
                             else f"{'BD' if cat == 'Bundle' else cat}-{code_}{self.ri(100, 990)}{self.r.choice(['', 'B', 'S', 'W', 'G'])}")
                    if model not in used:
                        break
                used.add(model)
                cost = round((lo + self.r.random() * (hi - lo)) / 5) * 5
                stock = 0 if self.r.random() < .07 else self.ri(25, 420)
                skus.append(Sku(sku_code=f"ME{10001 + i}", model_no=model, asin="B0" + self.code(8), ean="628" + "".join(str(self.ri(0, 9)) for _ in range(10)),
                                description=desc, category=cat, cost_h=cost * 100, free_stock=stock))
                i += 1
        Sku.objects.bulk_create(skus)
        self.skus = list(Sku.objects.all())
        Price.objects.bulk_create([Price(sku=s, cost_h=s.cost_h, valid_from=(self.now - timedelta(days=120)).date()) for s in self.skus])
        self.by_cat = {c: [s for s in self.skus if s.category == c] for c in CATS}

    # ---------- purchase orders ----------
    def mk_po(self, days_ago, n_lines=None):
        from orders.services import create_po
        od = self.t(-days_ago, -self.ri(1, 9))
        cat = self.r.choice(["PA", "PA", "DI", "DI", "TV", "HAV", "Bundle"])
        pool = self.by_cat[cat] + self.by_cat[self.r.choice(list(CATS))]
        chosen, seen = [], set()
        while len(chosen) < (n_lines or self.ri(2, 5)):
            s = self.r.choice(pool)
            if s.pk in seen:
                continue
            seen.add(s.pk)
            chosen.append((s, self.qty_for(s.category), s.cost_h))
        return create_po(self.code(8), self.r.choice(self.fcs), od, od + timedelta(days=2), chosen,
                         window_end=od + timedelta(days=self.ri(8, 12)), name="Vendor Central import", source="file upload")

    def accept_all(self, po):
        for l in po.lines.select_related("sku"):
            if l.sku.free_stock < l.qty_ordered:
                l.sku.free_stock = l.qty_ordered + self.ri(10, 60)
                l.sku.save(update_fields=["free_stock"])
            l.decision, l.qty_confirmed, l.reason = "accept", l.qty_ordered, ""
            l.save()

    def run_to(self, po, stage, short_delivery=0, no_delivery=False, bill_mismatch=False, slot_start=None):
        from billing.services import _invoice
        from fulfilment.services import _book_slot, _deliver, _submit_asn, make_delivery
        from orders.models import STAGES
        from orders.services import _book, _confirm, _release
        from payments.services import import_payment
        N = self.names
        target = STAGES.index(stage)
        at = po.order_date + timedelta(hours=self.ri(3, 20))
        if target >= 1:
            self.accept_all(po)
            _confirm(po, at, name=N["PIC"])
        if target >= 2:
            at += timedelta(hours=self.ri(2, 8))
            _book(po, at, name=N["Planning"], sap_order_no=str(next_number("sap_order", 4500018420)))
        if target >= 3:
            at += timedelta(hours=self.ri(3, 20))
            _release(po, at, name=N["Credit"], with_delivery=False)
            if not no_delivery:
                make_delivery(po, at + timedelta(hours=2), short=short_delivery, ship_date=at + timedelta(days=self.ri(1, 3)))
        if target >= 4:
            at += timedelta(hours=self.ri(4, 20))
            _submit_asn(po, at, name=N["PIC"])
        if target >= 5:
            at += timedelta(hours=self.ri(2, 10))
            if slot_start:
                po.shipment.ship_date = slot_start - timedelta(hours=10)
                po.shipment.save()
            _book_slot(po, at, f"CC{next_number('slot', 66120)}", slot_start or po.shipment.ship_date + timedelta(hours=self.ri(8, 14)),
                       "08:00–12:00", name=N["Logistics"])
        if target >= 6:
            at = max(at + timedelta(hours=1), po.shipment.slot_start + timedelta(hours=1))
            _deliver(po, at, name=N["Logistics"], bill_mismatch=bill_mismatch)
        if target >= 7:
            at += timedelta(hours=self.ri(4, 20))
            _invoice(po, at, name=N["PIC"])
        if target >= 8:
            inv = po.invoice
            pd = min(inv.invoice_date + timedelta(days=self.ri(28, 52)), self.t(-self.ri(1, 6)))
            import_payment(f"RMT-{next_number('payment', 9102200)}", pd, inv.invoice_no, inv.total_h, at=pd)
        po.refresh_from_db()
        return po

    def pos(self):
        from orders.services import refresh_suggestions
        from fulfilment.models import SapDelivery
        a = self.mk_po(1, 4); a.confirm_by = self.t(0, -2)
        b = self.mk_po(1, 5); b.confirm_by = self.t(0, 5)
        c = self.mk_po(0, 3); c.confirm_by = self.t(1, 6)
        d = self.mk_po(0, 2); d.confirm_by = self.t(1, 20)
        e = self.mk_po(0, 4); e.confirm_by = self.t(1, 22)
        f = self.mk_po(0, 3); f.confirm_by = self.t(2, 1)
        fresh = [a, b, c, d, e, f]
        for p in fresh:
            p.save()
        bl = list(b.lines.all()); bl[1].cost_h = round(bl[1].cost_h * 0.95); bl[1].save()
        cl = list(c.lines.all()); cl[0].cost_h = round(cl[0].cost_h * 0.96); cl[0].save()
        R = self.run_to
        R(self.mk_po(2), "confirmed"); R(self.mk_po(3), "confirmed")
        R(self.mk_po(3), "booked"); R(self.mk_po(4), "booked"); R(self.mk_po(4), "booked")
        R(self.mk_po(5), "released"); R(self.mk_po(6), "released", short_delivery=2); R(self.mk_po(5), "released", no_delivery=True)
        R(self.mk_po(7), "asn"); R(self.mk_po(8), "asn"); risk = R(self.mk_po(7), "asn")
        R(self.mk_po(9), "slot", slot_start=self.t(self.ri(0, 2), self.ri(4, 9))); R(self.mk_po(10), "slot", slot_start=self.t(self.ri(0, 2), self.ri(4, 9)))
        R(self.mk_po(12), "delivered", bill_mismatch=True); R(self.mk_po(12), "delivered"); R(self.mk_po(13), "delivered")
        for _ in range(8):
            R(self.mk_po(self.ri(16, 34)), "invoiced")
        for _ in range(14):
            R(self.mk_po(self.ri(40, 110)), "paid")
        # Open orders ship in the coming days.
        from orders.models import PurchaseOrder
        for p in PurchaseOrder.objects.filter(stage__in=["released", "asn", "slot"]):
            dlv = SapDelivery.objects.filter(po=p).first()
            if p.stage == "released" and dlv:
                dlv.ship_date = self.t(self.ri(1, 3), self.ri(0, 5)); dlv.save()
            if p.stage == "asn":
                sd = self.t(self.ri(2, 4), self.ri(0, 5)); p.shipment.ship_date = sd; p.shipment.save(); dlv.ship_date = sd; dlv.save()
            if p.stage == "slot":
                dlv.ship_date = p.shipment.ship_date; dlv.save()
        risk.shipment.ship_date = self.t(1, 4); risk.shipment.save()
        SapDelivery.objects.filter(po=risk).update(ship_date=self.t(1, 4))
        # Fresh POs: clean stock except two scripted shortfalls.
        for p in fresh:
            for l in p.lines.select_related("sku"):
                if l.sku.free_stock < l.qty_ordered:
                    l.sku.free_stock = l.qty_ordered + self.ri(20, 80); l.sku.save(update_fields=["free_stock"])
        for p, idx, lo, hi in ((b, 3, 4, 9), (e, 2, 3, 8)):
            l = list(p.lines.select_related("sku"))[idx]
            l.sku.free_stock = max(3, l.qty_ordered - self.ri(lo, hi)); l.sku.save(update_fields=["free_stock"])
        for p in fresh:
            refresh_suggestions(p)

    # ---------- payments ----------
    def payments(self):
        from core.services import audit
        from orders.models import PurchaseOrder
        from payments.models import Dispute, Payment
        from payments.services import import_payment
        inv = sorted([p.invoice for p in PurchaseOrder.objects.filter(stage="invoiced").select_related("invoice")], key=lambda i: i.invoice_date)
        # short-paid, open (flow F4)
        l0 = inv[0].lines.first()
        amt = round(min(l0.qty, 2) * l0.price_h * 1.15)
        import_payment(f"RMT-{next_number('payment', 9102200)}", self.t(-2, -5), inv[0].invoice_no, inv[0].total_h - amt, amt,
                       "Shortage — units received less than ASN", at=self.t(-2, -5))
        # short-paid, already disputed
        amt2 = round(inv[1].total_h * 0.04)
        p2 = import_payment(f"RMT-{next_number('payment', 9102200)}", self.t(-9, -3), inv[1].invoice_no, inv[1].total_h - amt2, amt2,
                            "Price — cost variance", at=self.t(-9, -3))
        p2.status = "disputed"; p2.save()
        d = Dispute.objects.create(case_no=f"DSP-{next_number('dispute', 41):04d}", type="price", ref=p2.payment_no, po=p2.po, amount_h=amt2,
                                   status="submitted", due=self.t(6), note="Invoice used the agreed cost. Amazon applied the old cost from the PO.")
        Dispute.objects.filter(pk=d.pk).update(created_at=self.t(-8))  # created_at is auto_now_add; match the opened event
        audit("dispute", d.case_no, "Dispute opened", name=self.names["Finance"], at=self.t(-8))
        audit("dispute", d.case_no, "Submitted to Amazon via Vendor Central contact form", name=self.names["Finance"], at=self.t(-7))
        audit("po", p2.po.po_no, f"Dispute {d.case_no} opened for SAR {round(amt2 / 100):,}", name=self.names["Finance"], at=self.t(-8))
        # unmatched payment (invoice number reformatted by Amazon)
        Payment.objects.create(payment_no=f"RMT-{next_number('payment', 9102200)}", remit_date=self.t(-1, -4),
                               invoice_ref="MEI-26-" + inv[2].invoice_no[-5:], paid_h=inv[2].total_h, status="unmatched", hint=inv[2].invoice_no)
        # an older won dispute
        paid = list(PurchaseOrder.objects.filter(stage="paid"))
        d2 = Dispute.objects.create(case_no=f"DSP-{next_number('dispute', 41):04d}", type="shortage", ref=f"RMT-{9102100 + self.ri(1, 90)}",
                                    po=paid[3], amount_h=276000, status="won", due=self.t(-50), note="Proof of delivery showed full cartons received.")
        Dispute.objects.filter(pk=d2.pk).update(created_at=self.t(-64))
        audit("dispute", d2.case_no, "Dispute opened", name=self.names["PIC"], at=self.t(-64))
        audit("dispute", d2.case_no, "Marked won. SAR 2,760 recovered", name=self.names["Finance"], at=self.t(-51))

    # ---------- promotions, debit notes, claims ----------
    def mk_promo(self, start_day, length, stage, cat=None, occ=None, n=None):
        from core.services import audit
        from promotions.models import Promotion
        from promotions.services import new_ref
        cat = cat or CAT_CYCLE[self.cc % len(CAT_CYCLE)]
        self.cc += 1
        local = timezone.localtime(self.now)
        start = local.replace(hour=6, minute=0) + timedelta(days=start_day)
        end = start + timedelta(days=length) - timedelta(hours=1)
        types = sorted({s.description for s in self.by_cat[cat]})
        p = Promotion.objects.create(mecl_ref=new_ref(), name=f"{occ or self.r.choice(OCC)} · {self.r.choice(types)}", category=cat,
                                     start=start, end=end, dn_due=end + timedelta(days=30), owner_name=self.names["Product"], stage="draft")
        created = start - timedelta(days=self.ri(10, 20))
        seen = set()
        while p.lines.count() < (n or self.ri(1, 4)):
            s = self.r.choice(self.by_cat[cat])
            if s.pk in seen:
                continue
            seen.add(s.pk)
            support = max(5, round(s.cost_h / 100 * (0.04 + self.r.random() * 0.08) / 5) * 5)
            exp = round(self.qty_for(cat) * self.ri(3, 9) / 10) * 10 or 40
            p.lines.create(sku=s, support_h=support * 100, expected_units=exp,
                           sold_units=max(10, round(exp * (0.8 + self.r.random() * 0.4))) if end < self.now else None)
        audit("promotion", p.mecl_ref, "Promotion received from product team", name=self.names["Product"], at=created)
        if stage != "draft":
            audit("promotion", p.mecl_ref, "Support per unit checked and formatted for Amazon", name=self.names["PIC"], at=created + timedelta(days=1))
            audit("promotion", p.mecl_ref, "Promotion submitted to Amazon", name=self.names["PIC"], at=created + timedelta(days=1, hours=2))
            p.stage = "submitted"
        if stage not in ("draft", "submitted"):
            p.agreement_no = str(71000000 + self.ri(10000, 99999))
            p.stage = "approved"
            audit("promotion", p.mecl_ref, f"Amazon approved. Agreement # {p.agreement_no} logged against {p.mecl_ref}", name=self.names["PIC"],
                  at=created + timedelta(days=4))
        p.save()
        return p

    def mk_dn(self, p, at, over=0, idx=0, agreement=None):
        """agreement: the number as Amazon sent it, when it differs from the promotion's (unlinked DN)."""
        from debitnotes.services import create_dn
        lines = [(l.sku, (l.sold_units if l.sold_units is not None else l.expected_units) + (over if i == idx else 0), l.support_h)
                 for i, l in enumerate(p.lines.select_related("sku"))]
        return create_dn(f"VCDN-{self.ri(10**6, 10**7 - 1)}", agreement or p.agreement_no, at, lines, name="Vendor Central import", at=at)

    def promos(self):
        from claims.services import _make_claim, _record_cn
        from debitnotes.services import _validate
        self.cc = 0
        P = self.names["PIC"]
        for _ in range(8):  # closed
            p = self.mk_promo(-self.ri(95, 130), self.ri(5, 10), "approved")
            dn = self.mk_dn(p, p.dn_due + timedelta(days=self.ri(1, 5)))
            _validate(dn, dn.dn_date + timedelta(days=2), name=P)
            c = _make_claim(p, dn.dn_date + timedelta(days=3), name=P)
            cd = c.sent_at + timedelta(days=self.ri(10, 20))
            _record_cn(c, f"CN-{next_number('credit_note', 552010)}", c.amount_h, cd, cd, name=self.names["Finance"])
        p = self.mk_promo(-82, 7, "approved", cat="PA")  # CN shortfall
        dn = self.mk_dn(p, p.dn_due + timedelta(days=2))
        _validate(dn, dn.dn_date + timedelta(days=1), name=P)
        c = _make_claim(p, dn.dn_date + timedelta(days=2), name=P)
        cd = c.sent_at + timedelta(days=12)
        _record_cn(c, f"CN-{next_number('credit_note', 552010)}", round(c.amount_h * 0.82), cd, cd, name=self.names["Finance"])
        notify(f"Credit note short on {c.claim_no}: SAR {round(c.gap_h / 100):,}", "bad", ("promo", p.mecl_ref, "claim"), at=cd)
        for d in (-60, -58, -54):  # claimed, waiting for CN
            p = self.mk_promo(d, 7, "approved")
            dn = self.mk_dn(p, p.dn_due + timedelta(days=self.ri(1, 3)))
            _validate(dn, dn.dn_date + timedelta(days=1), name=P)
            _make_claim(p, dn.dn_date + timedelta(days=2), name=P)
        for d in (-50, -46):  # DN validated, claim to send
            p = self.mk_promo(d, 6, "approved")
            dn = self.mk_dn(p, p.dn_due + timedelta(days=self.ri(1, 2)))
            _validate(dn, dn.dn_date + timedelta(days=1), name=P)
        # DN received: the BRD example (180 sold x SAR 50, DN charges 192 units -> SAR 600 over) and a clean one
        p = self.mk_promo(-44, 7, "approved", cat="DI", n=2, occ="Back to school")
        l0 = p.lines.first(); l0.support_h, l0.sold_units, l0.expected_units = 5000, 180, 200; l0.save()
        dn = self.mk_dn(p, self.t(-3, -4), over=12, idx=0)
        notify(f"DN {dn.dn_no} is SAR 600 above the agreement", "bad", ("promo", p.mecl_ref, "dn"), at=dn.dn_date)
        p = self.mk_promo(-41, 5, "approved", cat="PA")
        self.mk_dn(p, self.t(-2, -2))
        p = self.mk_promo(-66, 6, "approved", cat="HAV")  # DN overdue
        notify(f"No debit note yet for {p.mecl_ref}. It was due {timezone.localtime(p.dn_due):%d %b}", "warn", ("promo", p.mecl_ref, "dn"),
               at=p.dn_due + timedelta(days=15))
        wait = [self.mk_promo(-30, 7, "approved"), self.mk_promo(-24, 6, "approved"), self.mk_promo(-18, 5, "approved"), self.mk_promo(-12, 8, "approved", cat="TV")]
        # unlinked DN: agreement number with two digits swapped
        a = wait[0].agreement_no
        self.mk_dn(wait[0], self.t(-1, -6), agreement=a[:-2] + a[-1] + a[-2] if a[-1] != a[-2] else a[:-1] + "0")
        self.mk_promo(-6, 10, "approved", occ="National Day deals", cat="DI"); self.mk_promo(-4, 9, "approved", occ="National Day deals", cat="PA")
        self.mk_promo(-3, 7, "approved", occ="Weekend flash deals"); self.mk_promo(-1, 6, "approved", occ="Payday deals", cat="Bundle")
        self.mk_promo(12, 8, "approved", occ="Mega deals week"); self.mk_promo(55, 10, "approved", occ="White Friday", cat="TV")
        self.mk_promo(20, 7, "submitted", occ="Mega deals week", cat="PA"); self.mk_promo(55, 10, "submitted", occ="White Friday", cat="DI")
        self.mk_promo(60, 10, "draft", occ="White Friday", cat="HAV")

    def notices(self):
        from orders.models import PurchaseOrder
        from orders.services import po_issues
        from uploads.models import UploadBatch
        po = PurchaseOrder.objects.filter(stage="new", confirm_by__lt=self.now).first()
        if po:
            notify(f"PO {po.po_no} is past its confirm-by time", "bad", ("po", po.po_no, "lines"), at=po.confirm_by)
        for p in PurchaseOrder.objects.filter(stage="new"):
            if po_issues(p):
                notify(f"PO {p.po_no}: {po_issues(p)} lines need a decision", "warn", ("po", p.po_no, "lines"), at=p.order_date + timedelta(hours=1))
                break
        notify("Stock snapshot imported from SAP: 350 SKUs updated", "ok", at=self.t(0, -4))
        ids = list(Notification.objects.order_by("-at").values_list("id", flat=True))
        Notification.objects.filter(id__in=ids[4:]).update(read=True)
        admin = User.objects.get(username="admin")
        for tid, fn, rows, cr, up, err, who, at in [
            ("U4", "VC_PO_export.csv", 18, 6, 0, 0, "Faisal Al-Harbi", self.t(0, -3)), ("U3", "SAP_stock.xlsx", 350, 0, 350, 0, "Noura Al-Otaibi", self.t(0, -4)),
            ("U6", "VC_remittance.csv", 24, 24, 0, 1, "Priya Nair", self.t(-1, -5)), ("U8", "VC_debit_notes.csv", 6, 3, 0, 0, "Faisal Al-Harbi", self.t(-2, -1)),
            ("U2", "Amazon_cost_list_Q3.xlsx", 350, 0, 12, 0, "Faisal Al-Harbi", self.t(-9)), ("U1", "SKU_ASIN_master.xlsx", 350, 350, 0, 0, "System admin", self.t(-30))]:
            b = UploadBatch.objects.create(upload_type=tid, filename=fn, status="committed", rows_total=rows, created_count=cr, updated_count=up,
                                           error_count=err, uploaded_by=admin, uploaded_by_name=who)
            UploadBatch.objects.filter(pk=b.pk).update(created_at=at)
