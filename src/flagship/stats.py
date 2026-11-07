"""Experiment readout math with the standard library only.

Conversion per variant, absolute and relative lift against control, a 95% confidence interval on
the difference (unpooled normal approximation), a two-sided p-value from a pooled z test, and a
sample ratio mismatch (SRM) check: a chi-square goodness of fit of observed exposures against the
rollout weights. SRM p < 0.001 means the split is broken and the readout should not be trusted.
"""

from __future__ import annotations

import math


def norm_cdf(z: float) -> float:
    return 0.5 * math.erfc(-z / math.sqrt(2))


def _gammainc_upper_regularized(a: float, x: float) -> float:
    """Q(a, x) by series for x < a + 1, Lentz continued fraction otherwise (Numerical Recipes 6.2)."""
    if x <= 0:
        return 1.0
    gln = math.lgamma(a)
    if x < a + 1:
        term = total = 1.0 / a
        ap = a
        for _ in range(500):
            ap += 1
            term *= x / ap
            total += term
            if abs(term) < abs(total) * 1e-15:
                break
        return max(0.0, 1.0 - total * math.exp(-x + a * math.log(x) - gln))
    tiny = 1e-300
    b = x + 1 - a
    c = 1 / tiny
    d = 1 / b
    h = d
    for i in range(1, 500):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        d = tiny if abs(d) < tiny else d
        c = b + an / c
        c = tiny if abs(c) < tiny else c
        d = 1 / d
        delta = d * c
        h *= delta
        if abs(delta - 1) < 1e-15:
            break
    return math.exp(-x + a * math.log(x) - gln) * h


def chi2_sf(x: float, df: int) -> float:
    """Survival function of the chi-square distribution."""
    return _gammainc_upper_regularized(df / 2.0, x / 2.0)


def srm_check(observed: list[int], weights: list[float], threshold: float = 0.001) -> dict:
    total = sum(observed)
    wsum = sum(weights)
    pairs = [(o, w) for o, w in zip(observed, weights, strict=True) if w > 0]
    if total == 0 or len(pairs) < 2:
        return {"chi2": 0.0, "p_value": 1.0, "mismatch": False, "expected": [0] * len(observed)}
    expected = [total * w / wsum for w in weights]
    chi2 = sum((o - total * w / wsum) ** 2 / (total * w / wsum) for o, w in pairs)
    p = chi2_sf(chi2, len(pairs) - 1)
    return {
        "chi2": round(chi2, 4),
        "p_value": p,
        "mismatch": p < threshold,
        "expected": [round(e, 1) for e in expected],
    }


def compare(control: tuple[int, int], treatment: tuple[int, int], z_crit: float = 1.959964) -> dict:
    """control and treatment are (conversions, exposures)."""
    c_conv, c_n = control
    t_conv, t_n = treatment
    if c_n == 0 or t_n == 0:
        return {"diff": None, "lift": None, "ci_low": None, "ci_high": None, "p_value": None}
    pc, pt = c_conv / c_n, t_conv / t_n
    diff = pt - pc
    se = math.sqrt(pc * (1 - pc) / c_n + pt * (1 - pt) / t_n)
    pooled = (c_conv + t_conv) / (c_n + t_n)
    se_pooled = math.sqrt(pooled * (1 - pooled) * (1 / c_n + 1 / t_n))
    p_value = 2 * (1 - norm_cdf(abs(diff) / se_pooled)) if se_pooled > 0 else 1.0
    lift = diff / pc if pc > 0 else None
    out = {
        "diff": diff,
        "ci_low": diff - z_crit * se,
        "ci_high": diff + z_crit * se,
        "lift": lift,
        "lift_ci_low": (diff - z_crit * se) / pc if pc > 0 else None,
        "lift_ci_high": (diff + z_crit * se) / pc if pc > 0 else None,
        "p_value": p_value,
        "significant": p_value < 0.05,
    }
    return out


def readout(
    variation_names: list[str], exposures: list[int], conversions: list[int], weights: list[float], control: int = 0
) -> dict:
    rows = []
    for i, name in enumerate(variation_names):
        n, c = exposures[i], conversions[i]
        row = {"variation": i, "name": name, "users": n, "conversions": c, "rate": (c / n) if n else 0.0}
        if i != control:
            row.update(compare((conversions[control], exposures[control]), (c, n)))
        rows.append(row)
    return {"control": control, "variations": rows, "srm": srm_check(exposures, weights)}
