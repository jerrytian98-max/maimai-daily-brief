#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日一键跑：抓取 → 生成日报 →（可选）推送企业微信。

时间窗口（重要）
  默认 --hours 24。定时任务每天 22:00 跑，窗口就是**昨天 22:00 → 今天 22:00**，
  不是自然日 0:00–24:00。后者在 22:00 跑会漏掉昨天 22 点后的帖、
  又会把今天 22–24 点还没发生的时段算进来。

两条硬规则
  1. 抓取失败不能让日报「静默为空」。cookie 过期 / 接口变更时，
     仍用库里已有数据出报，并在报告顶部明确标注「数据时效性打折」。
  2. 增量需要「昨天收盘 vs 今天收盘」两份快照。只有一份时宁可写「暂缺」，
     也不写 0 —— 写 0 会让人误以为「今天没人讨论」。

用法
  python scripts/run_daily.py                    # 滚动 24h，出报
  python scripts/run_daily.py --push             # 出报并推送
  python scripts/run_daily.py --date 2026-01-01  # 补某一天（自然日）
  python scripts/run_daily.py --no-sync          # 只出报不抓取
  python scripts/run_daily.py --meta             # 附带排序依据（排查用）
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from config import ROOT, load as load_cfg, ensure_dirs  # noqa: E402

CLIENT = os.path.join(HERE, "maimai_client.py")
BRIEF = os.path.join(HERE, "brief.py")
PUSH = os.path.join(HERE, "push_wecom.py")


def pick_python() -> str:
    """挑一个装了 requests 的解释器。

    很多宿主环境自带的 python 没有 requests，而抓取必须用它。
    先试当前解释器，不行再按常见安装位置探测，最后原样返回
    （让子进程报出真实错误，而不是在这里静默失败）。
    """
    cands = [sys.executable]
    cands += [
        os.path.expandvars(r"%LOCALAPPDATA%\Python\bin\python.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python313\python.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python312\python.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python311\python.exe"),
        "/usr/bin/python3",
        "/usr/local/bin/python3",
    ]
    seen = set()
    for exe in cands:
        if not exe or exe in seen or not os.path.exists(exe):
            continue
        seen.add(exe)
        try:
            probe = subprocess.run([exe, "-c", "import requests"],
                                   capture_output=True, timeout=25)
            if probe.returncode == 0:
                return exe
        except Exception:  # noqa: BLE001
            continue
    return sys.executable


PY = pick_python()


def run(cmd, timeout=1800):
    """跑子进程，返回 (rc, stdout+stderr)。不用 shell，避免引号问题。"""
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, cwd=ROOT,
                           env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        out = (p.stdout or b"").decode("utf-8", "replace")
        err = (p.stderr or b"").decode("utf-8", "replace")
        return p.returncode, out + err
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT after %ss" % timeout


def sync(cfg) -> tuple:
    f = cfg["fetch"]
    rc, out = run([PY, "-X", "utf8", CLIENT, "sync",
                   "--webcid", cfg["circle"]["webcid"],
                   "--pages", str(f["pages"]),
                   "--max-comment-posts", str(f["max_comment_posts"]),
                   "--max-pages", str(f["max_pages"]),
                   "--sleep", str(f["sleep"])])
    stats = None
    for line in reversed(out.strip().splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                stats = json.loads(line)
                break
            except Exception:
                continue
    return rc, stats, out


def brief(cfg, date_arg=None, hours=None, meta=False, no_llm=False) -> tuple:
    cmd = [PY, "-X", "utf8", BRIEF, "--write", "--top", str(cfg["brief"]["top"])]
    if date_arg:
        cmd += ["--date", date_arg]
    else:
        cmd += ["--hours", str(hours or cfg["brief"]["hours"])]
    if meta:
        cmd.append("--meta")
    if no_llm:
        cmd.append("--no-llm")
    rc, out = run(cmd)
    path = None
    for line in out.strip().splitlines():
        if line.startswith("[saved]"):
            path = line.split("]", 1)[1].strip()
    return rc, path, out


def main() -> int:
    ap = argparse.ArgumentParser(description="同事圈日报一键跑")
    ap.add_argument("--date", default=None, help="补某个自然日（today / yesterday / YYYY-MM-DD）")
    ap.add_argument("--hours", type=float, default=None, help="滚动窗口小时数；默认取 config")
    ap.add_argument("--pages", type=int, default=None, help="feed 翻页数；默认取 config")
    ap.add_argument("--no-sync", action="store_true", help="只出报，不抓取")
    ap.add_argument("--push", action="store_true", help="生成后推送到企业微信")
    ap.add_argument("--meta", action="store_true", help="附带排序依据")
    ap.add_argument("--no-llm", action="store_true", help="跳过模型改写")
    args = ap.parse_args()

    ensure_dirs()
    cfg = load_cfg()
    if args.pages:
        cfg["fetch"]["pages"] = args.pages

    if not (cfg["circle"].get("webcid") or "").strip():
        print("✗ config.json 里 circle.webcid 还没填。"
              "打开公司同事圈页面，URL 里 webcid= 后面那串就是。")
        return 2

    started = dt.datetime.now()
    print("[%s] 开始" % started.strftime("%H:%M:%S"))

    note = ""
    if args.no_sync:
        note = "本次未抓取，仅用已有数据出报"
        print("  跳过抓取")
    else:
        rc, stats, out = sync(cfg)
        if stats:
            print("  抓取：帖子 %d 条（新 %d）｜ 新跟帖 %d 条" % (
                stats.get("posts_seen", 0), stats.get("posts_new", 0),
                stats.get("comments_new", 0)))
        else:
            print("  ⚠️ 抓取失败（rc=%s），用库里已有数据继续" % rc)
            tail = "\n".join(out.strip().splitlines()[-6:])
            print("     " + tail.replace("\n", "     "))
            note = "抓取本轮失败（cookie 可能过期），以下基于历史数据，时效性打折"

    rc, path, _out = brief(cfg, args.date, args.hours, args.meta, args.no_llm)
    if not path or not os.path.exists(path):
        print("  ✗ 日报生成失败（rc=%s）" % rc)
        return 2
    md = open(path, encoding="utf-8").read()

    if note:
        lines = md.splitlines()
        lines.insert(2, "> ⚠️ " + note)
        lines.insert(3, "")
        md = "\n".join(lines)
        open(path, "w", encoding="utf-8").write(md)

    print("  → %s（%d 字节）" % (path, len(md.encode("utf-8"))))

    if args.push:
        if not cfg.get("push", {}).get("enabled"):
            print("  （推送未启用：config.json 的 push.enabled 设为 true）")
        else:
            rc, out = run([PY, "-X", "utf8", PUSH, "--file", path, "--markdown"], timeout=120)
            print("  推送：%s" % ("成功" if rc == 0 else "失败 rc=%s %s" % (rc, out.strip())))
    else:
        print("  （未推送：加 --push 才会发企业微信）")

    print("[%s] 完成，用时 %.1fs" % (
        dt.datetime.now().strftime("%H:%M:%S"),
        (dt.datetime.now() - started).total_seconds()))
    print("\n" + "=" * 60 + "\n")
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
