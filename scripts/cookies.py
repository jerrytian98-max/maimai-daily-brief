#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cookie 来源：把「怎么拿到登录态」和「怎么用登录态」解耦。

脉脉的接口都要登录态。不同人环境不同，所以这里提供三种来源，按顺序尝试：

  1. config.json 里的 cookies_file —— 指向一个 JSON，键值对就是 cookie
     （推荐配合 scripts/chrome_cookies.py 从本机 Chrome 导出一次，之后定期重导）
  2. 环境变量 MAIMAI_COOKIES —— 直接放 JSON 字符串，适合 CI / 临时使用
  3. 项目根目录的 cookies.json —— 最省事的手工放置位置

**永远不要把 cookie 提交到版本库。** .gitignore 已经屏蔽常见路径。
"""
from __future__ import annotations

import json
import os
import sys

from config import ROOT

# 需要的 cookie 名（缺了会报错，比接口返回 401 更好排查）
REQUIRED = ("u", "access_token")
# csrftoken 用于 web 端接口，没有也能跑 App 网关兜底
NICE_TO_HAVE = ("csrftoken", "session")


def _from_env() -> dict:
    raw = os.environ.get("MAIMAI_COOKIES")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        print("[cookies] MAIMAI_COOKIES 解析失败：%s" % exc, file=sys.stderr)
        return {}


def _from_file(path: str) -> dict:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return {k: str(v) for k, v in data.items()}
    except Exception as exc:  # noqa: BLE001
        print("[cookies] %s 解析失败：%s" % (path, exc), file=sys.stderr)
        return {}


def load(cfg: dict | None = None) -> dict:
    """按优先级取 cookie，返回 dict；取不到就给出可操作的报错。"""
    cfg = cfg or {}
    candidates = []

    cf = (cfg.get("cookies_file") or "").strip()
    if cf:
        candidates.append(("config.cookies_file", cf))

    candidates.append(("环境变量 MAIMAI_COOKIES", None))

    for p in (
        os.path.join(ROOT, "cookies.json"),
        os.path.join(os.path.expanduser("~"), ".maimai-cookies.json"),
    ):
        candidates.append(("约定位置", p))

    found = {}
    for label, path in candidates:
        got = _from_env() if path is None else _from_file(path)
        if got:
            found = got
            print("[cookies] 来源：%s" % (path or label), file=sys.stderr)
            break

    if not found:
        raise SystemExit(
            "没有找到脉脉登录态。三种方式任选其一：\n"
            "  1) 运行 python scripts/chrome_cookies.py 从本机 Chrome 导出\n"
            "  2) 设置环境变量 MAIMAI_COOKIES='{\"u\":\"...\",\"access_token\":\"...\"}'\n"
            "  3) 把 cookie JSON 放到项目根目录的 cookies.json\n"
            "注意：cookie 等同账号密码，不要提交或外传。"
        )

    missing = [k for k in REQUIRED if not found.get(k)]
    if missing:
        raise SystemExit(
            "登录态缺少关键字段：%s。\n"
            "常见原因：只导出了部分 cookie，或登录已过期。请重新导出。" % ", ".join(missing)
        )
    absent = [k for k in NICE_TO_HAVE if not found.get(k)]
    if absent:
        print("[cookies] 缺少 %s，web 接口可能不可用（有 App 网关兜底）"
              % ", ".join(absent), file=sys.stderr)
    return found
