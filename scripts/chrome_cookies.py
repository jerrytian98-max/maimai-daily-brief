#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从本机 Chrome 导出脉脉登录 cookie，写到 cookies.json。

原理：Chrome 在 Windows 上用 DPAPI 保护一个 AES 主密钥（存在 Local State），
      再用它加密每条 cookie。本机当前用户能解开（因为 DPAPI 就是绑当前用户），
      所以不需要管理员权限，也不需要浏览器开着。

**这只读取你自己已登录的浏览器，导出的文件等同账号密码。**
请勿提交、勿外传、勿放在同步盘。

前置条件：Chrome 已登录脉脉，且打开过桌面版页面（移动版没有 csrftoken）。

依赖：pip install pywin32 cryptography

用法
  python scripts/chrome_cookies.py                # 默认 profile
  python scripts/chrome_cookies.py "Profile 1"    # 指定 profile
  python scripts/chrome_cookies.py --list         # 列出有哪些 profile
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import sqlite3
import sys
import tempfile

CHROME_DIRS = [
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data"),
    os.path.expanduser("~/Library/Application Support/Google/Chrome"),
    os.path.expanduser("~/.config/google-chrome"),
]

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(HERE), "cookies.json")


def find_chrome_dir() -> str:
    for d in CHROME_DIRS:
        if d and os.path.isdir(d):
            return d
    raise SystemExit("找不到 Chrome 用户数据目录。已尝试：\n  " + "\n  ".join(CHROME_DIRS))


def list_profiles(chrome: str) -> list:
    return sorted(d for d in os.listdir(chrome)
                  if os.path.isdir(os.path.join(chrome, d))
                  and os.path.isfile(os.path.join(chrome, d, "Network", "Cookies")))


def get_master_key(chrome: str) -> bytes:
    import win32crypt  # 仅 Windows 需要

    state_path = os.path.join(chrome, "Local State")
    if not os.path.isfile(state_path):
        raise SystemExit("找不到 Local State，无法取主密钥")
    with open(state_path, encoding="utf-8") as fh:
        state = json.load(fh)
    enc = state.get("os_crypt", {}).get("encrypted_key")
    if not enc:
        raise SystemExit("Local State 里没有 encrypted_key")
    blob = base64.b64decode(enc)
    if blob[:5] != b"DPAPI":
        raise SystemExit("主密钥前缀异常（期望 DPAPI）")
    return win32crypt.CryptUnprotectData(blob[5:], None, None, None, 0)[1]


def decrypt(key: bytes, blob: bytes) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if blob[:3] in (b"v10", b"v11"):
        return AESGCM(key).decrypt(blob[3:15], blob[15:], None).decode("utf-8", "replace")
    if blob[:3] == b"v20":
        raise RuntimeError("cookie 使用 v20（app-bound）加密，暂不支持；"
                           "可改用环境变量 MAIMAI_COOKIES 手工提供")
    return blob.decode("utf-8", "replace")


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    chrome = find_chrome_dir()

    if "--list" in sys.argv:
        print("Chrome 用户数据目录：", chrome)
        print("可用 profile：")
        for p in list_profiles(chrome):
            print("  -", p)
        return 0

    profile = args[0] if args else "Default"
    src = os.path.join(chrome, profile, "Network", "Cookies")
    if not os.path.isfile(src):
        src = os.path.join(chrome, profile, "Cookies")
    if not os.path.isfile(src):
        raise SystemExit("profile %r 下没有 Cookies 数据库；用 --list 看有哪些" % profile)

    tmp = os.path.join(tempfile.gettempdir(), "maimai_cookies_%s.db" % profile.replace(" ", "_"))
    shutil.copy2(src, tmp)

    key = get_master_key(chrome)
    conn = sqlite3.connect(tmp)
    rows = conn.execute(
        "SELECT host_key, name, encrypted_value FROM cookies WHERE host_key LIKE '%maimai%'"
    ).fetchall()
    conn.close()

    values, errors = {}, []
    for _host, name, enc in rows:
        try:
            values[name] = decrypt(key, enc)
        except Exception as exc:  # noqa: BLE001
            errors.append((name, str(exc)))

    if not values:
        print("没有解出任何脉脉 cookie。常见原因：\n"
              "  1) 这个 profile 没登录过脉脉（换 --list 里的其它 profile 试试）\n"
              "  2) Chrome 版本用了新的加密方式（见上面 errors）")
        return 1

    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(values, fh, ensure_ascii=False, indent=1)

    print("profile=%s 行数=%d 解出=%d 失败=%d" % (profile, len(rows), len(values), len(errors)))
    for name in ("u", "access_token", "csrftoken"):
        print("  %-14s %s" % (name, "OK" if values.get(name) else "缺失"))
    print("\n已写入：", OUT)
    print("⚠️ 这个文件等同账号密码：不要提交、不要外传。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
