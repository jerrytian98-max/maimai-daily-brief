#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""登录态保障：预检 → 失败则自愈 → 复验。

## 为什么需要这一层

脉脉的 token 在 Chromium 里存在两种状态：
  · 磁盘 cookie 库 —— 可能滞后（Chrome 会批量刷盘，内存里已换新、磁盘还是旧的）
  · 运行时内存  —— 真实生效的那份

11-21 那次断推 3 天就是这个原因：磁盘里是旧 token，脚本读磁盘 → 一直 401，
而浏览器里看着一切正常。所以「读磁盘」不能作为唯一来源。

## 三步策略

  1. **预检**：用现有 cookie 打一个轻量接口（count=1）。
     通过就结束，0 额外成本 —— 绝大多数日子走这条路。

  2. **自愈**：预检失败时，复制 Chrome profile 到临时目录，
     用 Playwright 打开同事圈页面。
     页面里的 JS 会用 session 换一个新 token 并写入 cookie，
     再从 `ctx.cookies()`（内存态，权威）读出来落盘。
     复制 profile 是为了不占用用户正在用的 Chrome。

  3. **复验**：用新 cookie 再打一次。还失败就判定「真失效」，
     需要人工重新登录 —— 这种情况任何自动手段都救不回来。

## 输出约定

最后一行永远是 JSON，供上游解析：
  {"ok": true,  "action": "none|disk|browser", "detail": "..."}
  {"ok": false, "action": "failed", "detail": "..."}
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
COOKIE_FILE = os.path.join(os.path.dirname(HERE), "cookies.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
FEED = "https://maimai.cn/groundhog/gossip/v3/feed"

CHROME_USER_DATA = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                                "Google", "Chrome", "User Data")
CHROME_EXE_CANDIDATES = [
    r"D:\Installation\Google\Chrome\Application\chrome.exe",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome",
                 "Application", "chrome.exe"),
]


# ------------------------------------------------------------------ cookie 读写
def load_cookies(path: str = COOKIE_FILE) -> dict:
    try:
        with open(path, encoding="utf-8-sig") as fh:
            return {k: str(v) for k, v in json.load(fh).items()}
    except Exception:  # noqa: BLE001
        return {}


def save_cookies(values: dict, path: str = COOKIE_FILE) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(values, fh, ensure_ascii=False, indent=1)


# ------------------------------------------------------------------ 预检
def probe(cookies: dict, webcid: str, timeout: int = 20) -> tuple:
    """打一个最小请求，返回 (ok, detail)。"""
    if not cookies.get("access_token") or not cookies.get("u"):
        return False, "cookie 缺少 access_token 或 u"
    s = requests.Session()
    for k, v in cookies.items():
        s.cookies.set(k, v, domain=".maimai.cn")
    s.headers.update({
        "User-Agent": UA,
        "Accept": "application/json",
        "Referer": "https://maimai.cn/company/gossip_discuss?webcid=" + webcid,
        "x-csrf-token": cookies.get("csrftoken", ""),
    })
    try:
        r = s.get(FEED, params={"webcid": webcid, "u": cookies.get("u"),
                                "channel": "www", "version": "4.0.0",
                                "page": 0, "count": 1}, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return False, "请求异常：%s" % str(exc)[:80]

    try:
        data = r.json()
    except Exception:  # noqa: BLE001
        return False, "HTTP %s，非 JSON 响应" % r.status_code

    if data.get("error_code"):
        return False, "error_code=%s %s" % (data.get("error_code"),
                                            data.get("error") or data.get("error_msg") or "")
    if not data.get("data"):
        return False, "返回 data 为空"
    return True, "ok（%d 条）" % len(data["data"])


# ------------------------------------------------------------------ 从磁盘导出
def export_from_disk(webcid: str) -> dict:
    """直接读 Chrome 的 cookie 库（快，但可能滞后）。

    chrome_cookies.py 会自己写 COOKIE_FILE，这里读回来即可。
    注意：它对 `u` / `access_token` 的判空是按解密结果来的，
    如果 Chrome 还没把内存里的新 token 刷盘，导出结果就是"有键但值空"，
    所以必须再用 probe 验一次，不能只看文件是否生成。
    """
    if not os.path.isfile(os.path.join(HERE, "chrome_cookies.py")):
        return {}
    try:
        subprocess.run([sys.executable, "-X", "utf8",
                        os.path.join(HERE, "chrome_cookies.py"), "Default"],
                       capture_output=True, timeout=120)
    except Exception:  # noqa: BLE001
        return {}
    got = load_cookies()
    # 空值当没导到
    if not got.get("access_token") or not got.get("u"):
        return {}
    return got


# ------------------------------------------------------------------ 浏览器自愈
def _find_chrome() -> str | None:
    for p in CHROME_EXE_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


def _copy_profile() -> str:
    """复制 Chrome Default profile 的关键文件到临时 user-data 目录。

    只拷 cookie / 本地存储 / 主密钥，跳过 Cache（几百 MB 且无用）。
    """
    tmp_root = tempfile.mkdtemp(prefix="chrome_clone_")
    src = os.path.join(CHROME_USER_DATA, "Default")
    dst = os.path.join(tmp_root, "Default")
    os.makedirs(dst, exist_ok=True)

    items = ["Network", "Local Storage", "Session Storage", "Preferences",
             "Login Data", "Web Data"]
    for item in items:
        s = os.path.join(src, item)
        d = os.path.join(dst, item)
        try:
            if os.path.isdir(s):
                shutil.copytree(s, d, ignore=shutil.ignore_patterns("*.lock", "LOCK"))
            elif os.path.isfile(s):
                shutil.copy2(s, d)
        except Exception:  # noqa: BLE001
            continue

    # Local State 里有解密主密钥，缺了 cookie 全解不开
    ls = os.path.join(CHROME_USER_DATA, "Local State")
    if os.path.isfile(ls):
        shutil.copy2(ls, os.path.join(tmp_root, "Local State"))
    return tmp_root


def heal_via_browser(webcid: str, headful: bool = False, wait_ms: int = 8000) -> dict:
    """复制 profile → 打开同事圈 → 触发 token 刷新 → 读内存 cookie。"""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001
        print("  [heal] Playwright 不可用：%s" % exc, file=sys.stderr)
        return {}

    try:
        profile = _copy_profile()
    except Exception as exc:  # noqa: BLE001
        print("  [heal] 复制 profile 失败：%s" % exc, file=sys.stderr)
        return {}

    url = "https://maimai.cn/company/gossip_discuss?webcid=" + webcid
    result = {}
    try:
        with sync_playwright() as p:
            launch = {"headless": not headful,
                      "args": ["--disable-blink-features=AutomationControlled"]}
            exe = _find_chrome()
            if exe:
                launch["executable_path"] = exe
            ctx = p.chromium.launch_persistent_context(profile, **launch)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception as exc:  # noqa: BLE001
                print("  [heal] 页面加载异常：%s" % str(exc)[:80], file=sys.stderr)
            page.wait_for_timeout(wait_ms)

            title = ""
            try:
                title = page.title()
            except Exception:  # noqa: BLE001
                pass
            cookies = ctx.cookies()
            result = {c["name"]: c["value"] for c in cookies
                      if "maimai" in (c.get("domain") or "")}
            print("  [heal] 页面标题：%s；取到 maimai cookie %d 条"
                  % (title or "(空)", len(result)), file=sys.stderr)
            ctx.close()
    except Exception as exc:  # noqa: BLE001
        print("  [heal] 浏览器自愈失败：%s" % str(exc)[:120], file=sys.stderr)
        return {}
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    return result


# ------------------------------------------------------------------ 主流程
def ensure(webcid: str, headful: bool = False, force: bool = False) -> dict:
    cookies = load_cookies()

    if not force:
        ok, detail = probe(cookies, webcid)
        if ok:
            return {"ok": True, "action": "none", "detail": "预检通过，%s" % detail}
        print("  [login] 预检失败：%s" % detail, file=sys.stderr)

    # 先试磁盘（便宜）
    fresh = export_from_disk(webcid)
    if fresh:
        ok, detail = probe(fresh, webcid)
        if ok:
            save_cookies(fresh)
            return {"ok": True, "action": "disk", "detail": "从磁盘重导后可用，%s" % detail}
        print("  [login] 磁盘版仍不可用：%s" % detail, file=sys.stderr)

    # 再上浏览器（贵但权威）
    print("  [login] 启动浏览器自愈…", file=sys.stderr)
    from_browser = heal_via_browser(webcid, headful=headful)
    if from_browser:
        ok, detail = probe(from_browser, webcid)
        if ok:
            save_cookies(from_browser)
            return {"ok": True, "action": "browser",
                    "detail": "浏览器自愈成功，%s" % detail}
        print("  [login] 浏览器版仍不可用：%s" % detail, file=sys.stderr)

    return {"ok": False, "action": "failed",
            "detail": "自动恢复失败，需要人工重新登录 maimai.cn"}


def main() -> int:
    ap = argparse.ArgumentParser(description="脉脉登录态预检与自愈")
    ap.add_argument("--webcid", required=True)
    ap.add_argument("--headful", action="store_true",
                    help="自愈时显示浏览器窗口（默认无头）")
    ap.add_argument("--force", action="store_true",
                    help="跳过预检，强制重新导出")
    args = ap.parse_args()

    res = ensure(args.webcid, headful=args.headful, force=args.force)
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res["ok"] else 3


if __name__ == "__main__":
    sys.exit(main())
