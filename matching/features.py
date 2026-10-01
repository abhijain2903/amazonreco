"""Similarity features for reconciliation matching. Pure functions (no database), like rules/engine.py.

Every feature returns a score in 0..1 plus, where useful, a short human reason that is shown next to a suggestion.
"""
import re
from itertools import combinations


def norm(s):
    """Upper-case letters and digits only: 'mei-2026/04312 ' -> 'MEI202604312'."""
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def digits(s):
    return re.sub(r"\D", "", str(s or ""))


def osa(a, b):
    """Edit distance counting an adjacent transposition as one edit (optimal string alignment)."""
    la, lb = len(a), len(b)
    d = [[0] * (lb + 1) for _ in range(la + 1)]
    for i in range(la + 1):
        d[i][0] = i
    for j in range(lb + 1):
        d[0][j] = j
    for i in range(1, la + 1):
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[la][lb]


def transposition(a, b):
    """1-based position of a single adjacent swap turning a into b, else None ('71060476' vs '71060467' -> 7)."""
    if len(a) != len(b) or a == b:
        return None
    diff = [i for i in range(len(a)) if a[i] != b[i]]
    if len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]]:
        return diff[0] + 1
    return None


def ref_similarity(a, b):
    """How alike two document references are, 0..1, with a reason. Tolerates case, punctuation, a trimmed prefix
    ('MEI-26-04312' vs 'MEI-2026-04312'), a typo or a swapped pair of digits."""
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0, ""
    if na == nb:
        return 1.0, "reference identical" if str(a).strip() == str(b).strip() else "same reference, different formatting"
    t = transposition(na, nb)
    if t:
        return 0.92, f"reference differs only by two swapped characters ({na[t - 1:t + 1]} / {nb[t - 1:t + 1]})"
    da, db = digits(a), digits(b)
    tail = 0
    while tail < min(len(da), len(db)) and da[-1 - tail] == db[-1 - tail]:
        tail += 1
    if tail >= 5:
        return 0.88, f"last {tail} digits of the reference match"
    if min(len(na), len(nb)) >= 6 and (na in nb or nb in na):
        return 0.85, "one reference contains the other"
    dist = osa(na, nb)
    s = max(0.0, 1 - dist / max(len(na), len(nb)))
    if dist == 1:
        return max(s, 0.86), "reference differs by one character"
    return s, (f"reference {round(s * 100)}% similar" if s >= 0.5 else "")


def amount_score(amount_h, target_h, tol_h):
    """Exact (within tolerance) scores 1. A payment below the target scores lower the further it is (short pay);
    above the target decays faster (overpayment is rare)."""
    if target_h <= 0:
        return 0.0, ""
    diff = amount_h - target_h
    if abs(diff) <= tol_h:
        return 1.0, "amount matches"
    rel = abs(diff) / target_h
    if diff < 0:
        return max(0.0, 1 - rel * 4), f"paid {rel * 100:.1f}% less than the total"
    return max(0.0, 1 - rel * 10), f"paid {rel * 100:.1f}% more than the total"


def window_score(days, lo, hi, soft=30):
    """1 inside [lo, hi] days, fading to 0 over `soft` days outside it."""
    if lo <= days <= hi:
        return 1.0
    gap = lo - days if days < lo else days - hi
    return max(0.0, 1 - gap / soft)


def jaccard(a, b):
    a, b = set(a), set(b)
    return len(a & b) / len(a | b) if a | b else 0.0


def subset_sum(target_h, items, tol_h, max_k=4, max_items=24):
    """Combinations of 2..max_k items whose amounts add up to target_h (within tolerance), smallest sets first.
    items: [(key, amount_h)]. Bounded so a large list cannot blow up: only the max_items closest in size are tried."""
    pool = sorted((i for i in items if 0 < i[1] <= target_h + tol_h), key=lambda i: -i[1])[:max_items]
    found = []
    for k in range(2, max_k + 1):
        for combo in combinations(pool, k):
            if abs(sum(a for _, a in combo) - target_h) <= tol_h:
                found.append([key for key, _ in combo])
                if len(found) >= 5:
                    return found
    return found


def weighted(parts):
    """parts: [(score 0..1, weight)] -> 0..100."""
    total = sum(w for _, w in parts)
    return round(sum(s * w for s, w in parts) / total * 100) if total else 0
