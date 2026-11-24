#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""集中管理配置：路径、同事圈、模型、推送。

设计原则
  1. **不硬编码任何账号、群、公司信息**。所有可变量都在 config.json 里，
     而这个文件默认不进版本库（见 .gitignore）。
  2. 首次运行时自动生成 config.example 的副本，用户填完就能用。
  3. 所有路径都相对于项目根目录推导，不依赖安装位置。
"""
from __future__ import annotations

import json
import os
import sys

# 项目根：本文件在 <root>/scripts/config.py
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config.json")
EXAMPLE_PATH = os.path.join(ROOT, "config.example.json")

DATA_DIR = os.path.join(ROOT, "data")
REPORTS_DIR = os.path.join(ROOT, "reports")
DB_PATH = os.path.join(DATA_DIR, "maimai.db")

DEFAULTS = {
    "circle": {
        # 同事圈 id：打开公司同事圈页面，URL 里的 webcid 参数就是它
        "webcid": "",
        # 圈名，仅用于报告标题，可留空
        "name": "",
    },
    "fetch": {
        "pages": 30,              # feed 翻几页；每页最多 10 条
        "max_comment_posts": 0,   # 0 = 不限制，全部帖都抓跟帖
        "max_pages": 8,           # 单帖一级评论最多翻几页
        "sleep": 0.3,             # 请求间隔（秒），别调太小，容易触发风控
    },
    "brief": {
        "hours": 24,              # 滚动窗口小时数
        "top": 5,                 # 要闻条数
        "show_meta": False,       # 是否显示排序依据（评论区点赞数等）
    },
    "llm": {
        "enabled": True,
        # 任何 OpenAI Chat Completions 兼容端点都可以
        "base_url": "http://127.0.0.1:8790/v1",
        "model": "deepseek-v4.1-flash",
        "api_key_env": "",
        # 思考型模型务必用 no_think：否则 reasoning 会吃光 token 预算
        "reasoning_effort": "no_think",
        "timeout": 180,
    },
    "push": {
        "enabled": False,
        "type": "wecom",          # wecom | none
        # 企业微信群机器人 webhook，形如
        # https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=XXXXXXXX
        "webhook": "",
        # 也可以只填 key，或写环境变量名从环境变量读
        "webhook_env": "MAIMAI_WECOM_WEBHOOK",
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path: str = CONFIG_PATH) -> dict:
    """读配置；缺失时给出明确指引而不是抛栈。

    一律用 utf-8-sig 打开：Windows 上用 PowerShell / 记事本编辑过的 JSON
    很容易带 BOM，用 utf-8 解析会直接报「Unexpected UTF-8 BOM」。
    """
    if not os.path.exists(path):
        if os.path.exists(EXAMPLE_PATH):
            with open(EXAMPLE_PATH, encoding="utf-8-sig") as fh:
                cfg = json.load(fh)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, ensure_ascii=False, indent=2)
            print("[config] 已生成 %s，请填写 circle.webcid 后再运行。" % path,
                  file=sys.stderr)
            return _deep_merge(DEFAULTS, cfg)
        raise SystemExit("缺少配置文件：%s" % path)
    try:
        with open(path, encoding="utf-8-sig") as fh:
            user = json.load(fh)
    except json.JSONDecodeError as exc:
        raise SystemExit(
            "config.json 解析失败（第 %d 行）：%s\n"
            "常见原因：多了或少了逗号、用了中文引号、注释没删干净。"
            "可以用 python -c \"import json;json.load(open('config.json',encoding='utf-8-sig'))\" 定位。"
            % (exc.lineno, exc.msg))
    return _deep_merge(DEFAULTS, user)


def ensure_dirs() -> None:
    for d in (DATA_DIR, REPORTS_DIR):
        os.makedirs(d, exist_ok=True)


def resolve_webhook(cfg: dict) -> str:
    """推送地址：优先 config，其次环境变量。"""
    hook = (cfg.get("push", {}).get("webhook") or "").strip()
    if hook:
        return hook
    env_name = cfg.get("push", {}).get("webhook_env") or ""
    if env_name:
        return (os.environ.get(env_name) or "").strip()
    return ""


if __name__ == "__main__":
    c = load()
    redacted = json.loads(json.dumps(c))
    # 打印时屏蔽密钥类字段
    if redacted.get("push", {}).get("webhook"):
        redacted["push"]["webhook"] = "<已配置，长度 %d>" % len(c["push"]["webhook"])
    if redacted.get("llm", {}).get("api_key"):
        redacted["llm"]["api_key"] = "<已配置>"
    print(json.dumps(redacted, ensure_ascii=False, indent=2))
