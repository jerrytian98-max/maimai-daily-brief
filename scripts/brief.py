#!/usr/bin/env python3
"""每日简报生成器 —— 从库里挑出「有结论、值得看」的内容，写成一份 1 屏日报。

核心判断：**热度 != 值得关注**
  最热的帖往往只是情绪共鸣（读完只会焦虑，不改变任何行为）；
  最有用的帖常常只有个位数赞，但写着「我已被沟通，12.30 走」这种第一人称事实。

两条硬规则
  1. 只输出**有结论**的：纯提问、评论区只有「同问/蹲/插眼」的一律淘汰。
     宁可当天只出 1 条，也不拿问句凑数。
  2. 每条附原帖链接，跟在正文同一行末尾。

排序逻辑
  value = 可信度 × (1 + 可行动性 + 0.5 × 影响面) + 增速 + 新颖度
  可信度是**乘数**：一条疯传但无来源的帖不该排第一。

结论识别（三级，可信度从高到低）
  A 楼主补充：楼主自己在评论区给出结果
  B 评论区高赞：带硬事实（数字/金额/流程）的高赞跟帖
  C 楼主自述：第一人称 + 具体时间/金额，且不是提问

  规则负责「筛」，模型负责「写」。规则剪出来常是半句话，
  所以还会过一次 summarize.py，让模型改写成一两句自洽的结论。

用法
  python scripts/brief.py                          # 最近 24 小时
  python scripts/brief.py --date yesterday --write
  python scripts/brief.py --hours 24 --meta        # 附带排序依据（排查用）
  python scripts/brief.py --no-llm                 # 跳过模型改写
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import maimai_client as watch  # noqa: E402
import summarize  # noqa: E402
from config import REPORTS_DIR, DATA_DIR, ensure_dirs  # noqa: E402

OUT_DIR = REPORTS_DIR
STATE = os.path.join(DATA_DIR, "briefed.json")


def circle_name() -> str:
    """报告落款用的圈名；没配就退回通用说法，避免写死公司名。"""
    from config import load as _load
    name = (_load().get("circle", {}).get("name") or "").strip()
    return "脉脉 " + name if name else "脉脉同事圈"

# ------------------------------------------------------------------ 词表
FIRST_PERSON = re.compile(
    r"我(已经|被|刚|今天|昨天|我们|们组|们部门|们中心|们这边|们司|们的)"
    r"|楼主(在|就是|自述|本人)|坐标|身边同事|收到通知|已经沟通|已约谈|等待第二轮"
    r"|我leader|我总监|我hrbp|我HRBP|我老板")
CROSS = re.compile(r"据(说|传|悉|可靠)|听说|打听到|朋友(说|在|已经)|同学(说|在)|内部消息|hrbp|HRBP|传达|口径")

DATE_RE = re.compile(
    r"12\s*[\.月]\s*\d{1,2}|1230|12月底|12月初|11月底|1月\d{1,2}|1月\d*日"
    r"|春节前|过年前|年前|年后|明年\d|明年上半年|下(周|月)|这(周|月)"
    r"|续签|保护期|缓冲期|PIP|pip|Q[1-4]|\d{1,2}月\d{1,2}日")
MONEY_RE = re.compile(
    r"n\s*\+\s*1|N\s*\+\s*1|2\s*[nN]|N\+3|赔偿|补偿|年终|礼包|服务奖|社平"
    r"|股票|期权|归属|涨薪|调薪|晋升|\d+\s*[wW万]\b|13薪|14薪|16薪|17薪|工资")
ACTION_RE = re.compile(
    r"活水|续签|转岗|调岗|回流|背星|PIP|pip|绩效|晋升|仲裁|一裁两审|沟通会"
    r"|名单|离职|last\s*day|交接|签字|录音|拒签")
RISK_RE = re.compile(
    r"举报|性骚扰|合规|审查|调查|媒体|外媒|舆情|删帖|乐问|乐闻|KM|km|敏感|泄密"
    r"|高压线|红线|通报")

# ------------------------------------------------------------------ 结论识别
# 用户的核心诉求：只推「有结论」的。纯提问、评论区只有「同问/蹲/插眼」的帖，
# 读完还是不知道答案 —— 那是噪音，不是精选。所以入选前必须先找到结论，
# 拿不到就淘汰，宁可当天只有 1 条，也不拿问句凑数。

FILLER_RE = re.compile(
    r"^(同问|同求|蹲|蹲一个|蹲后续|插眼|关注|留名|马克|mark|m|1|11|沙发|火钳|前排"
    r"|顶|顶一个|已阅|路过|吃瓜|哈哈哈*|笑死|太真实了|有道理|说得好|说得对|\+1"
    r"|等更新|求回复|求解答|不知道|不清楚|不懂|同想知道)"
    r"[\s。，、！？~～\.\W]*$",
    re.I,
)
BOT_RE = re.compile(r"脉脉(同事圈)?小助手|系统(提示|消息)|本条内容由系统")
# 「具体事实」：出现数字、比例、金额、时间、流程词 —— 空谈不算结论
FACT_RE = re.compile(
    r"\d{2,}|\d+\s*[%％]|n\s*\+\s*1|N\s*\+\s*1|赔偿|补偿|年终|礼包|股票|期权"
    r"|名单|沟通|约谈|通知|离职|走人|last\s*day|交接|签字|续签|活水|转岗|调岗"
    r"|绩效|背星|晋升|比例|折|裁员|优化|广进|外包|转正|offer",
    re.I,
)
# 提问式：结尾问号，或开头是「求/请问/有没有/是否/怎么/如何/多少/为什么」
Q_RE = re.compile(
    r"[？?]\s*$|^(求|请问|有人|有没有|是否|怎么|如何|多少|为什么|哪位|大佬|老哥)"
    r"|[吗嘛呢吧么]\s*[。！!~～]?$"
    r"|(我很好奇|好奇|想问问|问问大家|求大佬|求解答|有懂的吗)")
# 纯骂人/站队，不含信息
INSULT_RE = re.compile(
    r"无耻|垃圾|傻[逼xX]|智障|滚|去死|恶心|放屁|扯淡|胡说|造谣一张嘴|男男相护")


SENT_END = re.compile(r"[。！？；!?;]")


def clean_text(t, limit=90):
    """去掉 [吃惊] 这类表情标记，压成单行，并在截断处保持可读。

    直接 `t[:limit]` 会在半个词中间切断（「…天天加班，最」），
    读者会以为是原文残缺。所以：
      1. 若句号/问号/分号在 limit 的 60%~100% 之间，就在那里收尾
      2. 否则硬截到 limit 并补「…」，明确表示还有后文
    """
    t = re.sub(r"\[[^\]]{1,10}\]", "", t or "")
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) <= limit:
        return t

    window = t[:limit]
    last = None
    for m in SENT_END.finditer(window):
        if m.end() >= int(limit * 0.6):
            last = m.end()
    if last:
        return window[:last] + ("…" if len(t) > last else "")
    cut = window[: limit - 1].rstrip("，,、 ")
    if cut and not SENT_END.search(cut[-1]):
        cut += "…"
    return cut


def key_sentence(t, limit=100) -> str:
    """从长评论里挑最像「结论」的那一句。

    长贴多是议论（「我私以为…」），整段当结论读起来是半句话。
    所以按句切开，优先选带具体事实的句子；都不带就取第一句。
    """
    t = re.sub(r"\[[^\]]{1,10}\]", "", t or "")
    t = re.sub(r"\s+", " ", t).strip()
    # 短句里如果还嵌着问句（「你们听…了吗？他说…」），也要切开，
    # 否则会把提问的半句和答案粘成一条推给用户。
    if len(t) <= limit and not re.search(r"[？?]", t):
        return t
    parts = [s.strip() for s in re.split(r"[。！？!?；;\n]", t) if s.strip()]
    if not parts:
        return t[:limit]
    # 带事实的句子优先，其次取长度适中的
    scored = []
    for i, s in enumerate(parts):
        f = len(FACT_RE.findall(s))
        scored.append((f, -i, s))
    scored.sort(reverse=True)
    best = scored[0]
    if best[0] > 0 and len(best[2]) >= 8:
        return best[2][:limit]
    return parts[0][:limit]


# 「硬事实」：有这些才算说出了点具体东西。只有「绩效」「裁员」这类主题词不算。
HARD_FACT = re.compile(
    r"\d|n\s*\+\s*1|N\s*\+\s*1|2\s*n|2N|n\s*\+\s*2|社平|封顶|仲裁|赔偿|年终|礼包"
    r"|续签|活水|转岗|名单|交接|签字|last\s*day|背星|一星|晋升|调薪|转正|通知|约谈",
    re.I,
)
# 纯观点开头：没有数字的话，多半是感想不是结论
OPINION_HEAD = re.compile(r"^(是的|没错|确实|我觉得|我认为|个人认为|私以为|说白了|本质上)")


def is_conclusive(s: str) -> bool:
    """这一句拿出去，算不算一个「结论」。

    拦四类：问句、骂人、假设句、纯感想。
    之前出过「佬是说不晋升就不调薪么」「如果公司认为…那么如何识别…」
    「是的，没错，重要的是保持好奇心」这种，读者看完还是不知道答案。
    """
    s = (s or "").strip()
    if len(s) < 8:
        return False
    if Q_RE.search(s) or INSULT_RE.search(s):
        return False
    if not HARD_FACT.search(s):
        return False
    # 假设句只有在带「那…」的判断时才算结论（如「如果真一半人，那帖子早铺天盖地了」）
    if re.match(r"^(如果|假如|万一|要是|举个)", s) and "那" not in s:
        return False
    # 「如果…如何…」这类是反问句，不是结论
    if re.search(r"^(如果|假如|万一|要是|举个)", s) and re.search(r"(如何|怎么|是否|能不能)", s):
        return False
    # 句中带疑问副词的是反问/提问，不是结论（「为啥不用 qwen27b」读完没答案）
    # 除非明确给了因果或答案
    if re.search(r"(为啥|为什么|怎么|如何|是否|是不是|能不能|可不可以|难道|到底|多少)", s) and \
       not re.search(r"(所以|因此|结论|答案是|就是因为|原因是)", s):
        return False
    # 纯感想开头且没有数字，不算结论
    if OPINION_HEAD.match(s) and not re.search(r"\d", s):
        return False
    return True


def is_joke(s: str) -> bool:
    """玩梗、抖机灵、夸张到不可信 —— 这类不当结论。

    例子：「年终加 200 块」「那就都裁了吧」「减一半都是少的」。
    判据：金额/比例离谱 + 带调侃语气，或整句是口号式短句。
    """
    s = (s or "").strip()
    if len(s) < 6:
        return True
    # 200 块、20 块这种明显是梗的金额
    if re.search(r"(加|涨|给|发)\s*(\d{1,4})\s*(块|元|毛)", s):
        m = re.search(r"(\d+)\s*(块|元)", s)
        try:
            if int(m.group(1)) <= 1000 and "万" not in s:
                return True
        except Exception:
            pass
    # 「那就都裁了吧」「全干掉」这类口号
    if re.search(r"(那就都|全都|一律全|直接全)$", s) and "裁" in s:
        return True
    if re.search(r"(吧|呗|咯|哈)[。！!]?$", s) and len(s) < 14 and not re.search(r"\d", s):
        return True
    return False


RATIO_PAIR_RE = re.compile(r"(?<![\d:：])(\d{1,2})(\d{1,2})(?![\d:：%％])")
RATIO_CTX_RE = re.compile(r"(比例|股票|现金|期权|分成|配比|占比)")


def normalize_ratios(s: str) -> str:
    """把「46 / 37」这种省略冒号的比例还原成「4:6 / 3:7」。

    同事圈里常有人把 4:6 写成 46、3:7 写成 37，模型会照抄 ——
    直接推给用户会读成「四十六」。只在整句属于「比例语境」时才动手，
    且只改能被拆成两个 1-9 的两位数字，避免把 2026、200 块、50% 改坏。
    """
    if not RATIO_CTX_RE.search(s):
        return s

    def fix(m):
        a, b = m.group(1), m.group(2)
        if len(a) == 1 and len(b) == 1 and a != "0" and b != "0":
            return "%s:%s" % (a, b)
        return m.group(0)

    # 逐段处理，遇到明显的年份/金额/百分比就跳过那个 token
    out, i = [], 0
    for m in RATIO_PAIR_RE.finditer(s):
        out.append(s[i:m.start()])
        tail = s[m.end():m.end() + 2]
        head = s[max(0, m.start() - 2):m.start()]
        v = m.group(0)
        # 后面紧跟 % / 单位 / 年，或前面是金额单位 —— 不是比例
        if re.match(r"^\s*(%|％|块|元|万|个月|年|天|日|号|月)", tail) or \
           re.search(r"(\d|块|元|万)$", head) or \
           re.search(r"占\s*$", head):
            # 「股票占 54」这种是百分比，不是 5:4 的配比
            out.append(v)
        else:
            out.append(fix(m))
        i = m.end()
    out.append(s[i:])
    return "".join(out)


def answer_score(c) -> float:
    """一条跟帖是不是「真的给了答案」。0 = 废话。"""
    t = (c["text"] or "").strip()
    if not t or BOT_RE.search(t):
        return 0.0
    if FILLER_RE.match(t):
        return 0.0
    # 提问、反问、骂人都不算答案 —— 之前把「多少比例知道吗」当结论推出去过
    if Q_RE.search(t):
        return 0.0
    if INSULT_RE.search(t):
        return 0.0
    facts = len(FACT_RE.findall(t))
    # 没有任何具体事实（数字/流程/金额）的，最多算「观点」，不足以当结论
    if facts == 0:
        return 0.5 if (c["is_author"] and len(t) >= 15) else 0.0
    # 太短又没事实的（「那不就背星了嘛」）不配当结论
    if len(t) < 10:
        return 0.0

    s = 0.0
    if c["is_author"]:
        s += 3.0                        # 楼主亲自补充结果，最权威
    likes = c["likes"] or 0
    s += 1.5 if likes >= 2 else 0.0
    s += 1.5 if likes >= 5 else 0.0
    s += 1.0 if likes >= 10 else 0.0
    s += min(facts * 0.8, 2.4)
    s += 1.0 if 18 <= len(t) <= 120 else 0.0
    s -= 1.5 if len(t) > 260 else 0.0   # 超长文是议论，不是结论
    return round(s, 1)


def conclusion(p, answers):
    """找出这条帖的结论。返回 (结论文本, 出处标签)；找不到返回 None。

    三种成立方式，按可信度排序：
      A 楼主自己在评论区补充了结果 —— 最权威，是当事人给的答案
      B 高赞且带具体事实的跟帖 —— 群众验证过的答案
      C 帖子本身就是亲历自述（第一人称 + 具体时间/金额，且不是提问）
    """
    txt = p["text"]
    cands = sorted(answers, key=lambda c: -answer_score(c))

    for c in cands:
        # 楼主补充也必须带事实：光一句「那不就背星了嘛」不是结论
        if c["is_author"] and answer_score(c) >= 3.0:
            txt2 = key_sentence(c["text"], 110)
            if is_conclusive(txt2):
                return txt2, "楼主补充"

    for c in cands:
        if answer_score(c) >= 3.0:
            txt2 = key_sentence(c["text"], 110)
            if is_conclusive(txt2):
                return txt2, "评论区 %d 赞" % (c["likes"] or 0)

    if (FIRST_PERSON.search(txt) and (DATE_RE.search(txt) or MONEY_RE.search(txt))
            and not Q_RE.search(txt) and len(txt) >= 15):
        txt2 = key_sentence(txt, 110)
        if is_conclusive(txt2):
            return txt2, "楼主自述"
    return None


def substance(p) -> bool:
    """有没有实质内容：有结论，或评论区有像样的讨论。"""
    if p.get("_concl"):
        return True
    return max([answer_score(c) for c in p.get("_ans") or []] or [0]) >= 2.0


def post_url(p) -> str:
    """原帖链接。只有带 egid 的 detail 路径能打开（另两种实测 404）。"""
    return ("https://maimai.cn/community/gossip-detail/%s?webcid=%s&gid=%s&egid=%s"
            % (p["id"], p["webcid"], p["id"], p["egid"]))


def load_answers(conn):
    """全量跟帖，按 gid 分组 —— 结论识别不能只看窗口内的跟帖。"""
    out = collections.defaultdict(list)
    for r in conn.execute("SELECT gid, text, likes, is_author FROM comments"):
        out[r["gid"]].append({"text": r["text"] or "", "likes": r["likes"] or 0,
                              "is_author": bool(r["is_author"])})
    return out

ORG_PATTERNS = [
    ("WXG",   r"WXG|wxg|微信|小微|welm|企微"),
    ("IEG",   r"IEG|ieg|iegg|IEGG"),
    ("PCG",   r"PCG|pcg"),
    ("CSIG",  r"CSIG|csig|腾讯云|云智|云六"),
    ("TEG",   r"TEG|teg"),
    ("CDG",   r"CDG|cdg"),
    ("FiT",   r"FiT|fit|FIT"),
    ("混元",   r"混元|\bhy\d?\b|HY\d|AI\s*Infra|zeus"),
    ("工作室",  r"天美|光子|魔方|北极光|工作室|T1\b"),
    # 注意：不能只写「职能」——"提离职能把" 里的「职能」会被误判成部门，
    # 结果一条问股票归属的帖被标成「点名 公线/中台」。
    ("公线/中台", r"公线|公数|中台|职能线|职能部门|GLOBAL|global"),
    ("OVBU",  r"OVBU|ovbu|ovb\b|腾讯视频"),
    ("社交线",  r"社交线|社区|社死线"),
    ("S线",   r"S线|s线"),
    ("医疗",   r"医疗|医典"),
    # 层级也算「点到了具体主体」：谈总监/组长/GM 的帖自带落地价值，
    # 缺了这一条会被错判成「无组织、无可行动点」扔进噪音。
    ("管理层级", r"总监|组长|\bGM\b|\bgm\b|基干|中层|汇报线|嫡系"),
]
ORG_RES = [(name, re.compile(pat)) for name, pat in ORG_PATTERNS]


def orgs_of(text: str) -> list[str]:
    return [name for name, rx in ORG_RES if rx.search(text)]


RATIO_RE = re.compile(r"(\d{1,3})\s*(?:%|％|个点)|(\d{1,3})\s*成|对半|打五折|打七折")

CLUSTERS = {
    "裁员名单": r"裁员|广进|优化|名单|沟通|约谈|被干|毕业|CY|cy\b",
    "赔偿年终": r"n\s*\+\s*1|N\s*\+\s*1|2\s*[nN]|赔偿|补偿|年终|礼包|服务奖|社平|股票",
    "活水续签": r"活水|续签|转岗|调岗|回流",
    "职级绩效": r"晋升|10级|11级|T1\d|背星|星级|绩效|五星|三星",
    "组织中层": r"总监|组长|GM|gm|中层|基干|汇报|嫡系|山头|组织架构|汇报线|扁平",
    "AI混元":  r"混元|模型|算力|卡\b|GPU|训练|推理|元宝|workbuddy|\bwb\b|hy4",
    "业务竞争": r"字节|抖音|红果|短剧|阿里|qwen|豆包|seed|小红书|网易|快手",
    "舆情风险": r"举报|性骚扰|合规|媒体|外媒|政治|舆情|删帖|乐闻|乐问|高压线",
    "校招应届": r"校招|应届|保护期|实习生",
    "组织变革": r"合并|重组|架构调整|扁平|砍|拆|总办|linktime|发言|调整",
}
CLUSTER_RES = {k: re.compile(v) for k, v in CLUSTERS.items()}

SECTION_RULES = [
    ("风险与舆情",       ["舆情风险"]),
    ("需要你行动",       ["赔偿年终", "活水续签", "职级绩效", "校招应届"]),
    ("组织与业务动向",   ["裁员名单", "组织中层", "组织变革", "AI混元", "业务竞争"]),
]


# ------------------------------------------------------------------ 时间
def parse_loose(value: str | None, now: dt.datetime) -> dt.datetime | None:
    """把 '3小时前' / '昨天 12:30' / '11-12' / '刚刚' 解析成时间。"""
    if not value:
        return None
    v = str(value).strip()
    # DB 里存的是 ISO 时间（2026-11-15T15:17:34+08:00）；带时区，统一转本地 naive
    if len(v) >= 16 and v[4] == "-" and v[10] == "T":
        try:
            d = dt.datetime.fromisoformat(v)
        except ValueError:
            return None
        if d.tzinfo is not None:
            d = d.astimezone().replace(tzinfo=None)
        return d
    if v in ("刚刚", "just now"):
        return now
    m = re.match(r"^(\d+)\s*(分钟|小时|天|周)前$", v)
    if m:
        n, u = int(m.group(1)), m.group(2)
        return now - {"分钟": dt.timedelta(minutes=n), "小时": dt.timedelta(hours=n),
                      "天": dt.timedelta(days=n), "周": dt.timedelta(weeks=n)}[u]
    m = re.match(r"^昨天\s*(\d{1,2}):(\d{2})$", v)
    if m:
        return (now - dt.timedelta(days=1)).replace(
            hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
    m = re.match(r"^(\d{1,2})-(\d{1,2})$", v)
    if m:
        try:
            d = now.replace(month=int(m.group(1)), day=int(m.group(2)),
                            hour=12, minute=0, second=0, microsecond=0)
        except ValueError:
            return None
        return d - dt.timedelta(days=365) if d > now + dt.timedelta(days=1) else d
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", v)
    if m:
        try:
            return dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), 12)
        except ValueError:
            return None
    return None


# ------------------------------------------------------------------ 打分
def gather(conn, start: dt.datetime, end: dt.datetime, now: dt.datetime):
    """收集窗口内新发的帖 + 窗口内被新跟帖的帖。"""
    rows = conn.execute("SELECT * FROM posts").fetchall()
    posts, by_id = [], {}
    for r in rows:
        pa = parse_loose(r["posted_at"] or r["time_raw"], now)
        p = {
            "id": r["id"], "text": r["text"] or "", "author": r["author"] or "",
            "time_raw": r["time_raw"] or "", "likes": r["likes"] or 0,
            "cmts": r["cmts"] or 0, "spreads": r["spreads"] or 0,
            "ip": r["ip_loc"] or "", "posted": pa,
            # 链接要用：只有带 egid 的 detail 路径能打开（其余实测 404）
            "webcid": r["webcid"] or "", "egid": r["egid"] or "",
            "is_new": bool(pa and start <= pa < end),
            "new_cmts": 0, "new_likes": 0, "hot_cmts": [],
        }
        posts.append(p)
        by_id[p["id"]] = p

    # 窗口内的跟帖：按跟帖自己的时间算，而不是「本次 sync 抓到的时间」
    n_win = 0
    for c in conn.execute("SELECT gid, text, time_str, likes FROM comments").fetchall():
        ts = parse_loose(c["time_str"], now)
        if not ts or not (start <= ts < end):
            continue
        p = by_id.get(c["gid"])
        if not p:
            continue
        p["new_cmts"] += 1
        p["new_likes"] += c["likes"] or 0
        if c["text"]:
            p["hot_cmts"].append({"t": c["text"], "l": c["likes"] or 0})
        n_win += 1

    # 快照增量：需要「昨天收盘」和「今天收盘」两次同步才有真值。
    # 没有就返回 None —— 绝不能填 0，那会让人误以为「今天没人讨论」。
    day = end.date().isoformat()
    prev = (start - dt.timedelta(days=1)).date().isoformat()
    stats = {}
    for r in conn.execute("SELECT * FROM post_stats WHERE day>=?", (prev,)):
        stats.setdefault(r["gid"], {})[r["day"]] = r
    for p in posts:
        cur, old = stats.get(p["id"], {}).get(day), stats.get(p["id"], {}).get(prev)
        p["d_cmts"] = (cur["cmts"] - old["cmts"]) if (cur and old) else None
        p["d_likes"] = (cur["likes"] - old["likes"]) if (cur and old) else None
        p["d_spreads"] = (cur["spreads"] - old["spreads"]) if (cur and old) else None
    has_snap = any(p["d_cmts"] is not None for p in posts)
    return posts, n_win, has_snap


def score(p, cluster_size: int, seen_before: bool):
    txt = p["text"]
    reasons = []

    # ① 可信度（乘数 1.0–5.0）
    cred = 1.0
    if FIRST_PERSON.search(txt):
        cred += 2.0
        reasons.append("第一人称亲历")
    if CROSS.search(txt):
        cred += 0.4
        reasons.append("有来源转述")
    o = orgs_of(txt)
    if o:
        cred += 0.7
        reasons.append("点名 " + "/".join(o[:2]))
    if RATIO_RE.search(txt):
        cred += 0.4
    if cluster_size >= 3:
        cred += 0.8
        reasons.append(f"{cluster_size} 帖在说同一件事")
    if cluster_size >= 7:
        cred += 0.5
    cred = round(min(cred, 5.0), 1)

    # ② 可行动性（0–5）：读完会不会改你这周的行为
    act = 0.0
    if DATE_RE.search(txt):
        act += 1.6
        reasons.append("含时间点")
    if MONEY_RE.search(txt):
        act += 1.3
        reasons.append("涉及钱")
    if ACTION_RE.search(txt):
        act += 1.2
        reasons.append("涉及流程")
    if o:
        act += 0.7
    if RATIO_RE.search(txt):
        act += 0.6
        reasons.append("含具体比例")
    act = round(min(act, 5.0), 1)

    # ③ 影响面（0–5）
    scope = 1.0 + 0.9 * min(len(o), 3)
    if re.search(r"全公司|整个公司|所有 ?BG|每个 ?BG|全司|全腾讯", txt):
        scope += 1.5
        reasons.append("全公司级")
    scope = round(min(scope, 5.0), 1)

    # ④ 增量信号：优先用「窗口内真实的跟帖/点赞增量」，没有快照就退回「今日新增跟帖」
    vel = p["new_cmts"] * 3
    if p.get("d_likes") is not None:
        vel += max(p["d_likes"], 0) + max(p["d_cmts"] or 0, 0) * 3
    vel_bonus = min(vel / 40.0, 3.0)

    # ⑤ 新颖度
    nov = 0.0 if seen_before else 1.0

    # 老帖回温：不是今天发的，但今天涨了很多跟帖
    revived = (not p["is_new"]) and p["new_cmts"] >= 8
    if revived:
        reasons.append(f"旧帖今天涨 {p['new_cmts']} 评")

    value = cred * (1.0 + act + 0.5 * scope) + vel_bonus + 0.8 * nov
    return {
        "value": round(value, 1), "cred": cred, "act": act, "scope": scope,
        "vel": vel, "nov": nov, "reasons": reasons, "revived": revived,
    }


def section_of(clusters: list[str]) -> str:
    for label, keys in SECTION_RULES:
        if any(k in clusters for k in keys):
            return label
    return "其他"


def noise_reason(p) -> str:
    t = p["text"].strip()
    if len(t) <= 12:
        return "正文过短（纯情绪/梗）"
    if re.fullmatch(r"[\d\s\.\,，。？?！!a-zA-Z]+", t):
        return "无文字内容"
    if not (FIRST_PERSON.search(t) or CROSS.search(t) or orgs_of(t)):
        return "无来源、无组织、无可行动点"
    return "已被同主题更高分的帖覆盖"


RATIO_M = re.compile(
    r"(\d{1,3})\s*(?:%|％|个点)|(\d{1,3})\s*成|对半|打五折|打七折")
# 一个数字只有在「裁员语境」里才是裁员比例。
# 「拿到五星 90% 是安全的」里的 90% 是概率，不是比例——不加这层过滤会污染账本。
LAYOFF_CTX = re.compile(r"裁|优化|广进|走人|被干|毕业|CY|cy\b|指标|比例|砍")
NEG_CTX = re.compile(r"五星|四星|三星|星级|绩效|安全|概率|通过率|涨薪|调薪|晋升|保研|录取")
# 「打五折 / 对半」这种说法本身自带语境，不需要再找上下文
SELF_EVIDENT = re.compile(r"对半|五折|七折")
# 「裁员比例 50 以上」这类不带百分号的表述
ABOVE_M = re.compile(r"(?:裁|优化|广进|比例)[^，。；\n]{0,6}?(\d{1,3})\s*(?:%|％)?\s*以上")


def org_near(text: str, pos: int) -> str:
    """取数字前面最近的那个组织名；没有则整帖第一个组织名。"""
    best, best_start = None, -1
    for name, rx in ORG_RES:
        for m in rx.finditer(text):
            if m.start() <= pos + 6 and m.start() > best_start:
                best, best_start = name, m.start()
    if best:
        return best
    o = orgs_of(text)
    return o[0] if o else "(未点名)"


def claims_ledger(posts):
    """比例账本：同组织被爆出的比例互相对照，暴露互相矛盾的口径。

    日报的价值不只在「今天说了什么」，更在「今天有几种互相矛盾的说法」。
    同一个部门被爆 10% 和 50%，比任何单帖都更值得看 —— 说明至少有一方在传谣。
    """
    led = collections.defaultdict(list)
    for p in posts:
        if not p["is_new"] or not LAYOFF_CTX.search(p["text"]):
            continue
        txt = p["text"]
        hits = []  # (位置, 百分比)

        for m in RATIO_M.finditer(txt):
            window = txt[max(0, m.start() - 12): m.end() + 12]
            if not LAYOFF_CTX.search(window):
                continue
            if NEG_CTX.search(window) and not re.search(r"裁|优化|广进|走人", window):
                continue
            if m.group(1):
                hits.append((m.start(), int(m.group(1))))
            elif m.group(2):
                hits.append((m.start(), int(m.group(2)) * 10))
            else:
                hits.append((m.start(), 50))

        # 「打五折 / 对半」自带裁员语境，不需要再看上下文
        for m in SELF_EVIDENT.finditer(txt):
            hits.append((m.start(), 70 if "七折" in m.group(0) else 50))
        # 「据说混元裁员比例 50 以上」—— 没有百分号的表述，正则百分比抓不到
        for m in ABOVE_M.finditer(txt):
            hits.append((m.start(), int(m.group(1))))

        seen = set()
        for pos, n in hits:
            if not (0 < n <= 100):
                continue
            org = org_near(txt, pos)
            if (org, n) in seen:   # 同一帖里重复提到同一个数字只记一次
                continue
            seen.add((org, n))
            led[org].append({"pct": n, "gid": p["id"],
                             "likes": p["likes"], "cmts": p["cmts"]})
    out = []
    for org, items in led.items():
        vals = [i["pct"] for i in items]
        out.append({"org": org, "claims": items, "lo": min(vals), "hi": max(vals),
                    "spread": max(vals) - min(vals)})
    out.sort(key=lambda x: (-x["spread"], -len(x["claims"])))
    return out


# ------------------------------------------------------------------ 渲染
def render(posts, ledger, size, start, end, hours, top, n_win_cmts, has_snap=True):
    L = []
    tag = start.strftime("%Y-%m-%d") if hours >= 20 else f"{start:%m-%d %H:%M}~{end:%H:%M}"
    L += [f"# 同事圈日报 · {tag}", ""]
    new_cnt = sum(1 for p in posts if p["is_new"])
    old_hot = sum(1 for p in posts if not p["is_new"] and p["new_cmts"] >= 3)
    L += [f"- 当日新帖 **{new_cnt}** 条　｜　当日新跟帖 **{n_win_cmts}** 条"
          f"（分布在 {new_cnt + old_hot} 条帖上）", ""]

    if new_cnt < 15:
        L += [f"> ⚠️ 当天只采到 {new_cnt} 条新帖，明显低于日均（60–120 条）。"
              f"大概率是当天没有跑 sync，或分页深度不够 —— 数字要先打折扣再看。", ""]
    if not has_snap:
        L += ["> ℹ️ 还没有「昨天收盘 vs 今天收盘」两次快照，所以下面的**点赞/转发增量暂缺**；"
              "跟帖增量是按跟帖自身发布时间算的，仍然准确。想拿到完整增量，每天固定跑一次 sync 即可。", ""]

    # 必看必须是「今天发生的」：新发的，或今天被大量跟帖的旧帖。
    # 纯老帖即使历史分很高也不能占今天的头条，否则日报天天说同一件事。
    live = [p for p in posts if p["is_new"] or p["new_cmts"] >= 3]
    must = [p for p in live if p["_s"]["value"] >= 8][:3]
    if not must:
        must = sorted(live, key=lambda x: -x["_s"]["value"])[:3]

    # 今日主线：同一主题今天有几条帖在说 —— 这是「总结」的核心，比单帖更重要
    if size:
        hot = [(c, n) for c, n in size.most_common(6) if n >= 2]
        if hot:
            L += ["## 今日主线", ""]
            L += ["| 主题 | 今日帖数 | 代表帖 |", "|---|---|---|"]
            for cl, n in hot:
                rep = sorted([p for p in posts if p["is_new"] and cl in p["_cl"]],
                             key=lambda x: -x["_s"]["value"])[:1]
                if not rep:
                    continue
                r = rep[0]
                snippet = (r["text"].split("\n")[0] or "")[:40].replace("|", "/")
                L += [f"| {cl} | {n} | `{r['id']}` {snippet} |"]
            L += [""]

    L += ["## 今日必看", ""]
    for i, p in enumerate(must, 1):
        s = p["_s"]
        head = (p["text"].split("\n")[0] or "(空)")[:56].replace("|", "/")
        L += [f"**{i}. {head}**　`{p['id']}`", ""]
        L += [f"> {p['text'][:240].replace(chr(10), ' / ')}", ""]
        if p["hot_cmts"]:
            # 取今天新增里赞最高的一条（不是最长的），并过滤掉脉脉小助手的系统话术
            cand = [c for c in p["hot_cmts"] if "脉脉同事圈小助手" not in c["t"]]
            if cand:
                topc = sorted(cand, key=lambda c: (-c["l"], -len(c["t"])))[0]
                txt = topc["t"].replace("\n", " ")[:110]
                L += [f"> 💬 今日热评（赞{topc['l']}）：{txt}", ""]
        L += [f"- 为什么排前面：{'；'.join(dict.fromkeys(s['reasons'])) or '综合信号'}　"
              f"（可信度 {s['cred']} / 可行动性 {s['act']} / 今日增量 {s['vel']}）", ""]

    picked = {p["id"] for p in must}
    for sec in ["需要你行动", "组织与业务动向", "风险与舆情", "其他"]:
        items = [p for p in posts if p["_sec"] == sec and p["id"] not in picked
                 and p["_s"]["value"] >= (4.5 if sec != "其他" else 6.5)]
        items.sort(key=lambda x: -x["_s"]["value"])
        if not items:
            continue
        L += [f"## {sec}", ""]
        for p in items[:top]:
            s = p["_s"]
            head = (p["text"].split("\n")[0] or "(空)")[:52].replace("|", "/")
            flag = " 🔥回温" if s["revived"] else ""
            inc = f"+{p['new_cmts']} 评" if p["new_cmts"] else "—"
            L += [f"- **{head}**{flag}　`{p['id']}`　赞{p['likes']} 评{p['cmts']} 转{p['spreads']}"
                  f"（今日 {inc}）",
                  f"  {'；'.join(dict.fromkeys(s['reasons'])) or '—'}", ""]

    if ledger:
        L += ["## 口径对照（同组织被爆出的比例）", ""]
        L += ["| 组织 | 流传口径 | 出处 | 矛盾？ |", "|---|---|---|---|"]
        for e in ledger[:8]:
            vals = " / ".join(f"{c['pct']}%" for c in e["claims"])
            refs = " ".join(f"`{c['gid']}`" for c in e["claims"][:3])
            flag = f"⚠️ 差 {e['spread']} 个点" if e["spread"] >= 10 else "—"
            L += [f"| {e['org']} | {vals} | {refs} | {flag} |"]
        L += [""]

    low = sorted([p for p in posts if p["is_new"] and p["_s"]["value"] < 4.5],
                 key=lambda x: -x["_s"]["vel"])
    if low:
        L += ["## 已过滤（附原因，不做黑箱）", ""]
        L += ["| 帖子 | 赞/评/转 | 过滤原因 |", "|---|---|---|"]
        for p in low[:6]:
            head = (p["text"].split("\n")[0] or "(空)")[:38].replace("|", "/")
            L += [f"| {head} | {p['likes']}/{p['cmts']}/{p['spreads']} | {noise_reason(p)} |"]
        L += [""]

    L += ["---", "",
          f"窗口 {start:%m-%d %H:%M} → {end:%m-%d %H:%M}　"
          f"｜ 生成 {dt.datetime.now():%Y-%m-%d %H:%M}　｜ 数据源：{circle_name()}", ""]
    return "\n".join(L)


# ------------------------------------------------------------------ 精简版（1 屏）
def render_compact(posts, ledger, size, start, end, top=5, meta=False):
    """精选版：3 条「有结论」的消息 + 5 条要闻，每条带原帖链接。

    这一版的核心不是「排序」，是**淘汰**：
      · 纯提问、评论区只有同问/蹲/插眼的帖 —— 读完还是不知道答案，一律不要
      · 只留找到结论的：楼主补充 > 高赞实证跟帖 > 亲历自述
      · 宁可当天只出 1 条，也不拿问句凑够 3 条

    meta=False（默认）时**只留结论和链接**：不显示「评论区 N 赞 / 今日 +X 评 /
    可信度」这类排序依据，也不显示「已略过 N 条」。理由：这些是给写报告的人看的，
    读者只想知道发生了什么、去哪儿看原帖。要看这些就传 --meta。
    """
    # 标题带上窗口本身：滚动 24h 模式（22:00→22:00）下只写「11-16」
    # 会让人误以为是自然日，所以非整日窗口一律显示起止时间。
    if (start.hour, start.minute, start.second) == (0, 0, 0) and \
       (end.hour, end.minute, end.second) == (0, 0, 0):
        span = f"{start:%m-%d}"
    else:
        span = f"{start:%m-%d %H:%M}→{end:%m-%d %H:%M}"
    L = [f"# 同事圈日报 · {span}", ""]

    new_cnt = sum(1 for p in posts if p["is_new"])
    n_cmts = sum(p["new_cmts"] for p in posts)
    themes = [f"{c}({n})" for c, n in size.most_common(3) if n >= 2] if size else []
    L += [f"新帖 **{new_cnt}** ｜ 跟帖 **{n_cmts}** ｜ 主线：{' · '.join(themes) or '—'}", ""]

    live = [p for p in posts if p["is_new"] or p["new_cmts"] >= 3]
    # 「今日精选」只从**今天新发**的帖里选。
    # 之前把「老帖今天涨了评论」也算候选，结果连续三天头条是同一批老帖，
    # 日报变成复读机。回温的老帖归到下方单独小节。
    fresh = [p for p in live if p["is_new"]]
    with_concl = [p for p in fresh if p.get("_concl")]
    revived = [p for p in live if not p["is_new"] and p.get("_concl")]
    picked_ids = set()

    L += ["## 今日精选", ""]
    if not with_concl:
        L += ["_今天新帖里没有找到「有结论」的消息：全是提问或只有情绪共鸣，评论区没有给出答案。_", ""]
    for i, p in enumerate(with_concl[:3], 1):
        picked_ids.add(p["id"])
        txt, src = p["_concl"]
        s = p["_s"]
        # 链接跟在结论同一行末尾 —— 单独占一行会把 1 屏内容撑成 2 屏
        link = f" [原帖]({post_url(p)})"
        L += [f"**{i}. {txt}**{link}", ""]
        # 帖子里如果有背景（问题本身），压成一行放在结论下面。
        # 背景是问句时加「原问」前缀，避免读者把它当成结论的一部分。
        bg = clean_text(p["text"], 120)
        if bg and bg not in txt and len(p["text"]) > len(txt) + 10:
            label = "原问" if Q_RE.search(p["text"]) else "背景"
            L += [f"　{label}：{bg}", ""]
        if meta:
            why = [src]
            if p["new_cmts"] >= 3:
                why.append(f"今日 +{p['new_cmts']} 评")
            if s["cred"] >= 3.0 and src != "楼主自述":
                why.append(f"可信度 {s['cred']}")
            L += [f"　{' · '.join(why)}", ""]

    rest = sorted([p for p in fresh if p["id"] not in picked_ids and p.get("_concl")],
                  key=lambda x: -x["_s"]["value"])
    if rest:
        L += ["**要闻**", ""]
        short = {"需要你行动": "行动", "组织与业务动向": "动向", "风险与舆情": "风险"}
        for p in rest[:top]:
            txt, src = p["_concl"]
            L += [f"- [{short.get(p['_sec'], '其他')}] {clean_text(txt, 56)}　[原帖]({post_url(p)})"]
        L += [""]

    # 回温的老帖单独列出：确实在讨论，但不是今天发生的事，
    # 混进精选会让人误以为是新消息。
    if revived:
        revived.sort(key=lambda x: -x["new_cmts"])
        L += ["**旧帖回温**（今天涨了讨论，非今日新发）", ""]
        for p in revived[:3]:
            txt, src = p["_concl"]
            L += [f"- {clean_text(txt, 50)}（今日 +{p['new_cmts']} 评）　[原帖]({post_url(p)})"]
        L += [""]

    bad = [e for e in ledger if e["spread"] >= 10] if ledger else []
    if bad:
        txt = "；".join(f"{e['org']} " + "/".join(f"{c['pct']}%" for c in e["claims"])
                       for e in bad[:3])
        L += [f"**⚠️ 口径打架** {txt}", ""]

    # 被淘汰的和没结论的，只在 --meta 时列出来（给人看筛选透明度用，
    # 推送内容里不需要「已略过 41 条」这种统计）
    if meta:
        dropped = [p for p in live if not p.get("_concl")]
        if dropped:
            dropped.sort(key=lambda x: -x["_s"]["value"])
            L += [f"**已略过 {len(dropped)} 条**（无结论：纯提问 / 只有同问 / 评论区没答案）", ""]

    L += [f"— {start:%m-%d %H:%M}~{end:%m-%d %H:%M} · {circle_name()}", ""]
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=None, help="滚动窗口小时数；默认取 config")
    ap.add_argument("--date", default=None, help="自然日：today / yesterday / 2026-11-15")
    ap.add_argument("--top", type=int, default=None, help="要闻条数；默认取 config")
    ap.add_argument("--webcid", default=None, help="同事圈 id；不传则用 config.json")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--full", action="store_true", help="详细版（默认是精简版 1 屏）")
    ap.add_argument("--no-llm", action="store_true",
                    help="跳过模型改写，只用规则版结论（网关不可用时的兜底）")
    ap.add_argument("--meta", action="store_true",
                    help="附带排序依据（评论区 N 赞 / 今日 +X 评 / 可信度 / 已略过 N 条）；"
                         "默认不显示，推送内容只要结论和链接")
    args = ap.parse_args()

    # 命令行没给的值一律回落到 config.json，避免脚本里出现任何具体数字/圈 id
    from config import load as _load
    _cfg = _load()
    hours_arg = args.hours if args.hours is not None else _cfg["brief"]["hours"]
    top_n = args.top if args.top is not None else _cfg["brief"]["top"]
    meta_flag = args.meta or _cfg["brief"].get("show_meta", False)

    now = dt.datetime.now()
    if args.date:
        d = args.date.strip().lower()
        if d in ("today", "今天"):
            day = now.date()
        elif d in ("yesterday", "昨天"):
            day = (now - dt.timedelta(days=1)).date()
        else:
            day = dt.date.fromisoformat(args.date)
        start = dt.datetime.combine(day, dt.time(0, 0))
        end = start + dt.timedelta(days=1)
        hours = 24
    else:
        end = now
        start = end - dt.timedelta(hours=hours_arg)
        hours = hours_arg

    conn = watch.connect()
    posts, n_win_cmts, has_snap = gather(conn, start, end, now)
    answers = load_answers(conn)

    size = collections.Counter()
    for p in posts:
        p["_cl"] = [k for k, rx in CLUSTER_RES.items() if rx.search(p["text"])]
        for c in p["_cl"]:
            if p["is_new"]:
                size[c] += 1

    seen = set()
    if os.path.exists(STATE):
        try:
            seen = {int(k) for k in json.load(open(STATE, encoding="utf-8"))}
        except Exception:
            seen = set()

    for p in posts:
        p["_sz"] = max([size[c] for c in p["_cl"]] or [0])
        p["_s"] = score(p, p["_sz"], p["id"] in seen)
        p["_sec"] = section_of(p["_cl"])
        # 结论识别：用全量跟帖（不只是窗口内的），否则老帖的答案会看不见
        p["_ans"] = answers.get(p["id"], [])
        p["_concl"] = conclusion(p, p["_ans"])

    posts.sort(key=lambda x: -x["_s"]["value"])
    ledger = claims_ledger(posts)

    # 交给模型改写：只送「今天有动静且规则认为有结论」的帖，控制成本。
    # 模型给的结论优先，它否决（null）的就当无结论 —— 这是它比规则更准的地方。
    if not args.no_llm and summarize.available():
        live = [p for p in posts if (p["is_new"] or p["new_cmts"] >= 3)]
        cands = [{"id": p["id"], "text": p["text"],
                  "answers": p["_ans"],
                  "rule_concl": (p["_concl"][0] if p["_concl"] else None)}
                 for p in live[:30]]
        llm = summarize.rewrite(cands)
        if llm:
            kept = 0
            for p in posts:
                r = llm.get(p["id"])
                if not r:
                    continue        # 没送进模型的帖保持规则版结论
                if r["conclusion"]:
                    # 玩梗/抖机灵不算结论：「年终加200块」「那就都裁了吧」
                    if is_joke(r["conclusion"]):
                        p["_concl"] = None
                        continue
                    concl = normalize_ratios(r["conclusion"])
                    src = r["source"] or (p["_concl"][1] if p["_concl"] else "评论区")
                    p["_concl"] = (concl, src)
                    kept += 1
                else:
                    p["_concl"] = None   # 模型判定「没有明确答案」→ 淘汰
            print("[brief] 模型改写 %d 条，其中无结论被淘汰 %d 条"
                  % (kept, sum(1 for x in llm.values() if not x["conclusion"])))

    if args.json:
        out = [{
            "id": p["id"], "text": p["text"][:300], "posted": str(p["posted"]),
            "is_new": p["is_new"], "new_cmts": p["new_cmts"],
            "likes": p["likes"], "cmts": p["cmts"], "spreads": p["spreads"],
            "orgs": orgs_of(p["text"]), "clusters": p["_cl"], "section": p["_sec"],
            "score": p["_s"], "url": post_url(p),
            "conclusion": ({"text": p["_concl"][0], "source": p["_concl"][1]}
                           if p["_concl"] else None),
        } for p in posts[: top_n * 6]]
        print(json.dumps({"window": [str(start), str(end)], "ledger": ledger,
                          "posts": out}, ensure_ascii=False, indent=1))
        conn.close()
        return 0

    if args.full:
        md = render(posts, ledger, size, start, end, hours, top_n, n_win_cmts, has_snap)
    else:
        md = render_compact(posts, ledger, size, start, end, top_n, meta_flag)
    print(md)

    if args.write:
        os.makedirs(OUT_DIR, exist_ok=True)
        # 文件名跟着「窗口结束日」走：22:00 跑出的是 11-16 的日报（覆盖到 22:00），
        # 用 start 命名会变成 11-15，和用户认知的"今天的日报"对不上。
        path = os.path.join(OUT_DIR, f"daily-{end:%Y%m%d}.md")
        open(path, "w", encoding="utf-8").write(md)
        seen |= {p["id"] for p in posts if p["is_new"] and p["_s"]["value"] >= 4.5}
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        json.dump(sorted(seen), open(STATE, "w", encoding="utf-8"), ensure_ascii=False)
        print(f"\n[saved] {path}")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
