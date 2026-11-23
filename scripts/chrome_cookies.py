"""Extract maimai.cn cookies from the local Chrome profile (DPAPI + AES-GCM v10).

Writes <项目根>/cookies.json (name -> value).
Usage: python tools/maimai/chrome_cookies.py [ProfileDir]
"""
import base64
import json
import os
import shutil
import sqlite3
import sys
import tempfile

import win32crypt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

CHROME = os.path.join(os.environ["LOCALAPPDATA"], "Google", "Chrome", "User Data")
PROFILE = sys.argv[1] if len(sys.argv) > 1 else "Default"
# 导出到项目根目录的 cookies.json（.gitignore 已屏蔽）
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "cookies.json")


def get_master_key() -> bytes:
    with open(os.path.join(CHROME, "Local State"), encoding="utf-8") as fh:
        state = json.load(fh)
    blob = base64.b64decode(state["os_crypt"]["encrypted_key"])
    if blob[:5] != b"DPAPI":
        raise SystemExit("unexpected Local State key prefix")
    return win32crypt.CryptUnprotectData(blob[5:], None, None, None, 0)[1]


def decrypt(key: bytes, blob: bytes) -> str:
    if blob[:3] in (b"v10", b"v11"):
        return AESGCM(key).decrypt(blob[3:15], blob[15:], None).decode("utf-8", "replace")
    if blob[:3] == b"v20":
        raise RuntimeError("app-bound (v20) cookie encryption is not supported")
    return blob.decode("utf-8", "replace")


def main() -> None:
    src = os.path.join(CHROME, PROFILE, "Network", "Cookies")
    if not os.path.isfile(src):
        src = os.path.join(CHROME, PROFILE, "Cookies")
    if not os.path.isfile(src):
        raise SystemExit("no cookie db for profile %r" % PROFILE)
    tmp = os.path.join(tempfile.gettempdir(), "mm_cookies_%s.db" % PROFILE.replace(" ", "_"))
    shutil.copy2(src, tmp)

    key = get_master_key()
    con = sqlite3.connect(tmp)
    rows = con.execute(
        "SELECT host_key, name, encrypted_value FROM cookies WHERE host_key LIKE '%maimai%'"
    ).fetchall()
    con.close()

    values = {}
    errors = []
    for host, name, enc in rows:
        try:
            values[name] = decrypt(key, enc)
        except Exception as exc:  # noqa: BLE001
            errors.append((host, name, str(exc)))

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(values, fh, ensure_ascii=False, indent=1)

    print("profile=%s rows=%d decrypted=%d errors=%d" % (PROFILE, len(rows), len(values), len(errors)))
    for e in errors:
        print("  ERR", e)
    print("out:", OUT)
    for name in ("u", "csrftoken", "access_token", "session"):
        v = values.get(name, "")
        print("  %-12s len=%-4d %s" % (name, len(v), "OK" if v else "MISSING"))


if __name__ == "__main__":
    main()
