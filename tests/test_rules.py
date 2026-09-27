"""Rules R1-R12 are pure functions: no database needed."""
from datetime import timedelta

from django.utils import timezone

from rules import engine
from rules.engine import Cfg


def cfg(**over):
    c = Cfg.defaults()
    for r, v in over.items():
        c.rules[r].update(v)
    return c


def test_r1_price_within_tolerance():
    c = cfg()
    assert engine.price_check(100_00, 100_00, c) == (True, 0)
    assert engine.price_check(100_40, 100_00, c)[0]           # 0.40 SAR, under 1 SAR / 0.5%
    ok, diff = engine.price_check(94_00, 100_00, c)
    assert not ok and diff == -6_00


def test_r1_can_be_switched_off():
    assert engine.price_check(50_00, 100_00, cfg(R1={"enabled": False}))[0]


def test_r1_unknown_agreed_price_passes():
    assert engine.price_check(123_00, 0, cfg())[0]


def test_r2_and_suggestions():
    c = cfg()
    assert engine.suggest(100_00, 100_00, 10, 50, c) == ("accept", 10, "")
    assert engine.suggest(100_00, 100_00, 10, 4, c) == ("partial", 4, "Limited stock")
    assert engine.suggest(100_00, 100_00, 10, 0, c) == ("reject", 0, "Out of stock")
    assert engine.suggest(80_00, 100_00, 10, 50, c)[0] == "reject"
    assert engine.line_tone(100_00, 100_00, 10, 4, c) == "warn"


def test_r3_confirm_state():
    now = timezone.now()
    c = cfg()
    assert engine.confirm_state(now - timedelta(minutes=1), now, c) == "overdue"
    assert engine.confirm_state(now + timedelta(hours=3), now, c) == "due_soon"
    assert engine.confirm_state(now + timedelta(days=2), now, c) == "ok"


def test_r4_asn_must_equal_delivery():
    c = cfg()
    assert engine.asn_line(10, 10, 10, c) == {"asn_ok": True, "del_ok": True}
    assert engine.asn_line(10, 8, 8, c) == {"asn_ok": True, "del_ok": False}
    assert not engine.asn_line(10, 8, 10, c)["asn_ok"]


def test_r5_slot_risk():
    now = timezone.now()
    assert engine.slot_at_risk(False, now + timedelta(hours=20), now, cfg())
    assert not engine.slot_at_risk(True, now + timedelta(hours=20), now, cfg())
    assert not engine.slot_at_risk(False, now + timedelta(days=5), now, cfg())


def test_r6_invoice_line():
    c = cfg()
    assert engine.invoice_line(10, 10, 100_00, 100_00, c) == {"qty_ok": True, "price_ok": True}
    assert not engine.invoice_line(10, 12, 100_00, 100_00, c)["qty_ok"]


def test_r7_payment_with_r12_tolerance():
    c = cfg()
    assert engine.payment_match(999_50, 1000_00, c) == "matched"   # within 1 SAR
    assert engine.payment_match(990_00, 1000_00, c) == "short"


def test_r9_dn_overdue():
    now = timezone.now()
    assert engine.dn_overdue(now - timedelta(days=16), now, cfg())
    assert not engine.dn_overdue(now - timedelta(days=10), now, cfg())


def test_r10_brd_example():
    """BRD example: 180 units sold at SAR 50 support; Amazon charges 192 units -> SAR 600 too much."""
    now = timezone.now()
    r = engine.dn_check([{"sku": 1, "units": 192, "rate_h": 50_00}], {1: {"support_h": 50_00, "sold": 180}},
                        now, now - timedelta(days=30), cfg())
    assert r["expected_h"] == 9000_00 and r["charged_h"] == 9600_00 and r["variance_h"] == 600_00
    assert not r["ok"] and not r["lines"][0]["units_ok"]


def test_r10_model_not_in_promo_and_early_dn():
    now = timezone.now()
    r = engine.dn_check([{"sku": 2, "units": 5, "rate_h": 10_00}], {1: {"support_h": 10_00, "sold": 5}}, now, now + timedelta(days=1), cfg())
    assert not r["ok"] and not r["lines"][0]["in_promo"] and not r["date_ok"]


def test_r11_cn_check():
    assert engine.cn_check(1000_00, 1000_00, cfg()) == "closed"
    assert engine.cn_check(1000_00, 820_00, cfg()) == "shortfall"
    assert engine.cn_check(1000_00, 820_00, cfg(R11={"enabled": False})) == "closed"
