"""Integer minor units only. The model never does arithmetic; this module does."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

_AMOUNT_RE = re.compile(r"^\(?-?\$?\s*[\d,]*\.?\d+\)?$")


def parse_amount(raw: str | float | int) -> int:
    """'$1,234.50' / '(45.00)' / '-12' / 12.5 -> minor units (int). Raises ValueError."""
    if isinstance(raw, int):
        return raw * 100
    if isinstance(raw, float):
        return int((Decimal(str(raw)) * 100).quantize(Decimal("1")))
    s = str(raw).strip().replace("USD", "").strip()
    if not s or not _AMOUNT_RE.match(s.replace(" ", "")):
        raise ValueError(f"not an amount: {raw!r}")
    neg = s.startswith("(") and s.endswith(")") or s.startswith("-") or "-$" in s
    s = s.strip("()").replace("$", "").replace(",", "").replace("-", "").strip()
    try:
        v = int((Decimal(s) * 100).quantize(Decimal("1")))
    except InvalidOperation as e:
        raise ValueError(f"not an amount: {raw!r}") from e
    return -v if neg else v


def fmt(minor: int, currency: str = "USD") -> str:
    sign = "-" if minor < 0 else ""
    m = abs(int(minor))
    sym = "$" if currency == "USD" else f"{currency} "
    return f"{sign}{sym}{m // 100:,}.{m % 100:02d}"


def pct_change(cur: int, prev: int) -> float | None:
    if prev == 0:
        return None
    return round((cur - prev) / abs(prev) * 100, 1)
