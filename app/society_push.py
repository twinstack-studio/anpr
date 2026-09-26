"""Web Push notifications for the society gate app (resident approval requests).

Works on Android (Chrome, Edge, Firefox) and desktop browsers, and on iPhone once the app is added to the home screen.
The VAPID key pair is created on first use and kept in runtime/society/vapid.json.
"""
import base64
import json
import threading
from pathlib import Path

VAPID_FILE = Path(__file__).resolve().parent.parent / "runtime" / "society" / "vapid.json"
CONTACT = "mailto:hello@twinstackstudio.com"
_lock = threading.Lock()
_keys: dict | None = None


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def keys() -> dict:
    global _keys
    with _lock:
        if _keys is None:
            if VAPID_FILE.exists():
                _keys = json.loads(VAPID_FILE.read_text())
            else:
                from cryptography.hazmat.primitives import serialization
                from cryptography.hazmat.primitives.asymmetric import ec

                priv = ec.generate_private_key(ec.SECP256R1())
                pem = priv.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()).decode()
                pub = priv.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
                _keys = {"private_pem": pem, "public": _b64(pub)}
                VAPID_FILE.parent.mkdir(parents=True, exist_ok=True)
                VAPID_FILE.write_text(json.dumps(_keys))
                VAPID_FILE.chmod(0o600)
    return _keys


def public_key() -> str:
    return keys()["public"]


def send(subs: list[dict], payload: dict, on_gone=None):
    """Sends one notification to each subscription in a background thread. on_gone(endpoint) is called for
    subscriptions the browser has dropped, so they can be deleted."""
    if not subs:
        return

    def run():
        from py_vapid import Vapid
        from pywebpush import WebPushException, webpush

        vapid = Vapid.from_pem(keys()["private_pem"].encode())
        data = json.dumps(payload)
        for s in subs:
            try:
                webpush({"endpoint": s["endpoint"], "keys": s["keys"]}, data, vapid_private_key=vapid,
                        vapid_claims={"sub": CONTACT}, ttl=600, timeout=10)
            except WebPushException as e:
                code = getattr(e.response, "status_code", None)
                if code in (404, 410) and on_gone:
                    on_gone(s["endpoint"])
            except Exception:  # noqa: BLE001  (network errors must not break the gate)
                pass
    threading.Thread(target=run, daemon=True).start()
