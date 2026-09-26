"""Pure helpers for the society gate app (no database, no GPU), so they can be unit-tested anywhere."""
import hashlib
import hmac
import os

from .plates import normalize


def clean_plate(raw: str) -> str:
    text, _ = normalize(str(raw or ""))
    return text


def _core(p: str) -> str:
    parts = p.split("-")
    return parts[0] + parts[-1] if len(parts) == 3 else p.replace("-", "")


def same_plate(a: str | None, b: str | None) -> bool:
    """Same plate, allowing one wrong OCR character and ignoring the small year digits (LEA-20-4060 = LEA-4060)."""
    if not a or not b:
        return False
    a, b = _core(a), _core(b)
    if a == b:
        return True
    if len(a) != len(b) or len(a) < 6:  # short plates (AZF-52) must match exactly
        return False
    return sum(x != y for x, y in zip(a, b)) <= 1


def hash_pw(pw: str) -> str:
    salt = os.urandom(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${h.hex()}"


def check_pw(pw: str, stored: str) -> bool:
    try:
        _, salt, h = stored.split("$")
        got = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
        return hmac.compare_digest(got.hex(), h)
    except Exception:  # noqa: BLE001
        return False
