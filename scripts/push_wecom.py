#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""企业微信群机器人推送。

企业微信 markdown 消息上限 4096 字节，超出会被截断，所以这里统一压到 3900 以内。

Webhook 获取方式（企业微信 App / 网页版均适用）：
  群聊 → 右上角「...」→ 群机器人 → 添加 → 复制 Webhook 地址
  地址形如 https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxxxx

**Webhook 等同群密码**：拿到的人可以往群里发任意消息。
所以本工具只从 config.json 或环境变量读，不写死在代码里，也不要提交到版本库。

用法
  python scripts/push_wecom.py --file reports/daily-20260101.md --markdown
  python scripts/push_wecom.py "纯文本消息"
  echo "内容" | python scripts/push_wecom.py - --markdown

退出码：0 成功 / 2 未配置 / 3 接口返回错误 / 4 网络错误
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import load as load_cfg, resolve_webhook  # noqa: E402

TEXT_LIMIT = 2048
MARKDOWN_LIMIT = 4096
SAFE_BUDGET = 3900


def truncate_bytes(text: str, limit: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[: max(0, limit - 3)].decode("utf-8", "ignore") + "..."


def send(hook: str, payload: dict) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        hook, data=body, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    # 显式空 ProxyHandler：本机有系统代理时 qyapi 直连才通
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read().decode("utf-8", "replace") or "{}")
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))


def main() -> int:
    ap = argparse.ArgumentParser(description="企业微信群机器人推送")
    ap.add_argument("text", nargs="?", help="消息内容；传 - 表示从 stdin 读")
    ap.add_argument("--markdown", action="store_true",
                    help="以 markdown 发送（支持 **加粗**、[链接](url)、> 引用）")
    ap.add_argument("--file", metavar="PATH", help="从文件读内容")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要发送的报文")
    args = ap.parse_args()

    cfg = load_cfg()
    if not cfg.get("push", {}).get("enabled"):
        print(json.dumps({"ok": False,
                          "error": "推送未启用：把 config.json 的 push.enabled 设为 true"},
                         ensure_ascii=False))
        return 2

    hook = resolve_webhook(cfg)
    if not hook.startswith("https://qyapi.weixin.qq.com/"):
        print(json.dumps({"ok": False,
                          "error": "webhook 未配置或格式不对（应以 "
                                   "https://qyapi.weixin.qq.com/ 开头）"}, ensure_ascii=False))
        return 2

    if args.file:
        with open(args.file, encoding="utf-8-sig") as fh:
            content = fh.read()
    elif args.text == "-":
        content = sys.stdin.read()
    elif args.text:
        content = args.text
    else:
        print(json.dumps({"ok": False, "error": "没有消息内容"}, ensure_ascii=False))
        return 2

    if args.markdown:
        payload = {"msgtype": "markdown",
                   "markdown": {"content": truncate_bytes(content, SAFE_BUDGET)}}
    else:
        payload = {"msgtype": "text",
                   "text": {"content": truncate_bytes(content, TEXT_LIMIT)}}

    if args.dry_run:
        print(json.dumps({"ok": True, "dryRun": True, "payload": payload}, ensure_ascii=False))
        return 0

    result = send(hook, payload)
    if result.get("errcode") != 0:
        print(json.dumps({"ok": False, "errcode": result.get("errcode"),
                          "errmsg": result.get("errmsg")}, ensure_ascii=False))
        return 3
    print(json.dumps({"ok": True, "msgtype": payload["msgtype"],
                      "bytes": len(content.encode("utf-8"))}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
