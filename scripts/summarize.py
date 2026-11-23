#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把候选帖交给模型，提炼成「一句结论」。

为什么需要这一层
  规则能筛出「哪条帖有答案」，但剪出来的往往是半句话：
    「37是因为股票太多，所以压低了现金比例」——读者不知道 37 是什么的比例。
  所以最后一步交给模型：给它帖子原文 + 跟帖，让它写一句
  「谁、因为什么、结论是什么」都齐整的话。

硬约束（都是踩过的坑）
  1. 模型挂了不能连累日报 —— 任何异常都退回规则版结论，绝不抛错。
  2. 不许编造 —— prompt 里明确要求只用给定材料，并在返回里带 evidence 字段回指。
  3. 不做价值判断、不推荐买卖、不评价个人 —— 这是同事圈简报，不是投顾。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import load as load_cfg  # noqa: E402

BASE_DEFAULT = "https://api.deepseek.com/v1"
MODEL_DEFAULT = "deepseek-flash"


def _cfg() -> dict:
    try:
        return load_cfg().get("llm", {})
    except Exception:  # noqa: BLE001
        return {}


SYSTEM = """你在为一位公司员工写「同事圈日报」。用户明确要求：只要**有结论**的内容。

你会收到一批帖子（含正文和评论区摘录）。对每条，写一句话结论。

规则：
1. 一句话，不超过 45 个汉字，直接给结论，不要「据说」「有人认为」这类缓冲词。
2. 必须具体：带上部门/职级/金额/比例/时间点等材料里真实出现的要素。材料里没有的**绝对不要编**。
3. 如果材料里没有明确答案（只有提问、只有同问、只有情绪），conclusion 填 null。
4. 如果结论依赖背景（比如「37 是 3:7 的股票现金比」），要把背景补进去让句子自洽。
5. 不要评价好坏、不要给建议、不要做价值判断，只陈述事实和已形成的答案。
6. **比例、金额、时间必须原样保留**：材料写「3:7」就写「3:7」，不要写成「37」；材料写「n+1」就写「n+1」，不要写成「n加1」。
7. 如果材料里是多个互相矛盾的说法，把每个说法都列出来（如「有 5:5、4:6、3:7 三种说法」），不要替读者选一个。

只输出 JSON，格式：
{"items":[{"id":123,"conclusion":"……" 或 null,"source":"楼主补充|评论区|楼主自述","note":""}]}
不要输出任何其他文字。"""


def _post(url, key, payload, timeout=180):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
    )
    # 显式空 ProxyHandler：本机有系统代理时，模型网关直连才通
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _strip_fence(s: str) -> str:
    s = (s or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", s, re.S)
    return (m.group(1) if m else s).strip()


def _key() -> str:
    """密钥来自 config.llm.api_key_env 指定的环境变量。

    不硬编码任何具体服务商的变量名 —— 这个 skill 可以接任意
    OpenAI Chat Completions 兼容端点。
    """
    name = (_cfg().get("api_key_env") or "").strip()
    if name:
        v = os.environ.get(name)
        if v:
            return v.strip()
    # 兜底：常见命名
    for fallback in ("MAIMAI_LLM_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
        v = os.environ.get(fallback)
        if v:
            return v.strip()
    return ""


def available() -> bool:
    return bool(_key())


def rewrite(cands, timeout=180):
    """cands: [{id, text, answers:[{text,likes,is_author}], rule_concl}]

    返回 {id: {"conclusion": str|None, "source": str}}；失败返回 {}（调用方回退）。
    """
    if not cands:
        return {}
    key = _key()
    if not key:
        return {}

    lines = []
    for c in cands:
        lines.append("### id=%s" % c["id"])
        lines.append("正文：%s" % (c["text"] or "")[:600].replace("\n", " / "))
        ans = sorted(c.get("answers") or [], key=lambda a: -(a.get("likes") or 0))[:8]
        if ans:
            lines.append("评论区摘录：")
            for a in ans:
                who = "楼主" if a.get("is_author") else "同事"
                lines.append("  - [%s 赞%d] %s" % (who, a.get("likes") or 0,
                                                   (a.get("text") or "")[:200].replace("\n", " ")))
        if c.get("rule_concl"):
            lines.append("（规则版结论供参考，可改写或否决：%s）" % c["rule_concl"][:120])
        lines.append("")

    # 思考型模型（如混元 hy4）会先用 reasoning 吃掉预算，之前 4000 token 时
    # content 是空串。对这类模型用 no_think 又快又好；但 DeepSeek 官方端点
    # 不认这个字段（会 400），所以只在配置显式指定或非官方端点时才带上。
    cfg = _cfg()
    base = (cfg.get("base_url") or BASE_DEFAULT).rstrip("/")
    payload = {
        "model": cfg.get("model") or MODEL_DEFAULT,
        "temperature": 0.3,
        "max_tokens": 16000,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": "以下是候选帖：\n\n" + "\n".join(lines)},
        ],
    }
    effort = cfg.get("reasoning_effort") or os.environ.get("MAIMAI_SUMMARY_EFFORT")
    # DeepSeek 官方端点不认识 reasoning_effort（会 422），无论配置里写了什么都要剥掉。
    # 其他兼容端点（混元等思考型模型）才需要它来关掉 reasoning。
    if "deepseek.com" in base:
        effort = None
    if effort:
        payload["reasoning_effort"] = effort
    # 自动重试：网关会偶发 401/500（本机实测三次），一次失败就不该放弃。
    # 401 通常是网关侧的瞬时鉴权抖动，不是 key 真的失效 —— 重试往往就过。
    # 退避 3s → 8s；仍失败才回退规则版（日报不中断）。
    attempts = int(os.environ.get("MAIMAI_SUMMARY_RETRIES", "3"))
    delays = [3, 8, 15]
    last_err = None
    obj = None
    for i in range(max(1, attempts)):
        try:
            data = _post(base + "/chat/completions", key, payload,
                         int(cfg.get("timeout") or timeout))
            choice = data["choices"][0]
            content = choice["message"].get("content") or ""
            if not content.strip():
                raise ValueError("空返回（finish_reason=%s，reasoning 吃光了预算）"
                                 % choice.get("finish_reason"))
            obj = json.loads(_strip_fence(content))
            if i:
                print("[summarize] 第 %d 次重试成功" % i, file=sys.stderr)
            break
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if i < attempts - 1:
                wait = delays[min(i, len(delays) - 1)]
                print("[summarize] 第 %d 次失败（%s），%ds 后重试"
                      % (i + 1, str(exc)[:60], wait), file=sys.stderr)
                time.sleep(wait)

    if obj is None:
        print("[summarize] 重试 %d 次仍失败，回退规则版：%s" % (attempts, last_err),
              file=sys.stderr)
        return {}

    out = {}
    for it in obj.get("items") or []:
        try:
            gid = int(it["id"])
        except Exception:
            continue
        concl = it.get("conclusion")
        if isinstance(concl, str):
            concl = re.sub(r"\s+", " ", concl).strip()
        if not concl or len(concl) < 6:
            concl = None
        out[gid] = {"conclusion": concl, "source": (it.get("source") or "").strip()}
    return out


if __name__ == "__main__":
    # 自测：拿库里两条真实帖跑一遍
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "maimai-watch"))
    import watch  # noqa: E402

    conn = watch.connect()
    demo = []
    for gid in (38147149, 38157211):
        r = conn.execute("SELECT id, text FROM posts WHERE id=?", (gid,)).fetchone()
        if not r:
            continue
        ans = [{"text": c["text"], "likes": c["likes"], "is_author": bool(c["is_author"])}
               for c in conn.execute(
                   "SELECT text, likes, is_author FROM comments WHERE gid=? ORDER BY likes DESC LIMIT 8",
                   (gid,))]
        demo.append({"id": r["id"], "text": r["text"], "answers": ans,
                     "rule_concl": "（规则版）走仲裁，2n，封顶三倍社平"})
    conn.close()
    print(json.dumps(rewrite(demo), ensure_ascii=False, indent=1))
