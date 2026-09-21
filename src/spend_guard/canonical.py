"""RFC 8785 (JSON Canonicalization Scheme) serialisation and hashing.

`input_hash` and `event_hash` must not change when a caller reorders keys or
re-indents its JSON, so every hash in Spend Guard goes through `canonicalize`.

Python's ``json.dumps(sort_keys=True)`` is close but wrong in two places that
matter, so both are implemented here:

* JCS orders object keys by UTF-16 code unit, Python orders by code point.
  The two disagree once a key contains a non-BMP character.
* JCS serialises numbers with the ECMAScript ``Number::toString`` algorithm.
  Python's ``repr`` disagrees above 1e16 (``1e+16`` vs ``10000000000000000``)
  and on small exponents (``1e-07`` vs ``1e-7``).
"""

from __future__ import annotations

import hashlib
import math
from decimal import Decimal
from typing import Any

__all__ = ["canonicalize", "sha256_hex", "hash_json"]

_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def _utf16_key(text: str) -> bytes:
    """Sort key giving UTF-16 code-unit order, as JCS requires."""
    return text.encode("utf-16-be", errors="surrogatepass")


def _string(text: str) -> str:
    out = ['"']
    for char in text:
        escape = _ESCAPES.get(char)
        if escape is not None:
            out.append(escape)
        elif char < "\x20":
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def _number(value: float | int) -> str:
    """ECMAScript Number::toString, which is what JCS specifies for numbers."""
    if isinstance(value, int):
        return str(value)
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON cannot represent NaN or Infinity")
    if number == 0.0:
        return "0"  # also collapses -0.0, as ECMAScript does
    sign = "-" if number < 0 else ""
    # repr() gives the shortest round-tripping decimal, which is the `s` and `n`
    # the ECMAScript algorithm is defined in terms of.
    _, digit_tuple, exponent = Decimal(repr(abs(number))).as_tuple()
    digits = "".join(str(d) for d in digit_tuple)
    stripped = digits.rstrip("0")
    if stripped:
        exponent += len(digits) - len(stripped)
        digits = stripped
    else:  # pragma: no cover - the zero case returned above
        digits = "0"
    k = len(digits)
    n = exponent + k  # value == 0.<digits> * 10**n
    if k <= n <= 21:
        return sign + digits + "0" * (n - k)
    if 0 < n <= 21:
        return sign + digits[:n] + "." + digits[n:]
    if -6 < n <= 0:
        return sign + "0." + "0" * (-n) + digits
    mantissa = digits if k == 1 else digits[0] + "." + digits[1:]
    power = n - 1
    return f"{sign}{mantissa}e{'+' if power >= 0 else '-'}{abs(power)}"


def _write(value: Any, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, str):
        out.append(_string(value))
    elif isinstance(value, (int, float)):
        out.append(_number(value))
    elif isinstance(value, (list, tuple)):
        out.append("[")
        for index, item in enumerate(value):
            if index:
                out.append(",")
            _write(item, out)
        out.append("]")
    elif isinstance(value, dict):
        out.append("{")
        for index, key in enumerate(sorted(value, key=_utf16_key)):
            if not isinstance(key, str):
                raise TypeError("JCS requires string object keys")
            if index:
                out.append(",")
            out.append(_string(key))
            out.append(":")
            _write(value[key], out)
        out.append("}")
    else:
        raise TypeError(f"cannot canonicalize {type(value).__name__}")


def canonicalize(value: Any) -> str:
    """Return the RFC 8785 canonical form of a JSON-compatible value."""
    out: list[str] = []
    _write(value, out)
    return "".join(out)


def sha256_hex(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def hash_json(value: Any) -> str:
    """Canonicalize then hash, so key order and whitespace never change the hash."""
    return sha256_hex(canonicalize(value))
