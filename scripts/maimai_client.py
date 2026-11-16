#!/usr/bin/env python3
"""脉脉同事圈抓取器 —— 帖子 + 全部跟帖（一级评论 / 楼中楼回复），入库并支持增量。

数据链路（均已验证可用，无需浏览器、无需 Playwright）
  帖子列表 : GET  https://maimai.cn/groundhog/gossip/v3/feed?webcid=..&u=..&page=N&_csrf=..
             备用：https://open.taou.com/maimai/gossip/v3/feed?...&webcid=..
             （App 网关，用 access_token，不需要 csrf）
  一级评论 : POST https://maimai.cn/community/gossip-detail/<gid>?webcid=..&gid=..&egid=..
             header  next-action: <getGossipComments>       body  [{"gid":gid,"egid":egid,"page":N}]
  二级回复 : 同上
             header  next-action: <getGossipLv2Comments>    body  [{"page":N,"cid":cid,"egid":egid}]

  server action id 会随前端发版变化，脚本会自动从 Next.js chunk 里重新发现并缓存，
  不需要每次发版都改代码。

用法
  python scripts/maimai_client.py sync    --webcid <id> [--pages 30] [--max-comment-posts 0]
  python scripts/maimai_client.py backfill --webcid <id>     # 补齐历史欠账
  python scripts/maimai_client.py status

注意
  这是给「已登录用户读自己能看的内容」用的低量工具。请保持默认的请求间隔，
  不要改成并发高频 —— 既容易被风控，也不是这个工具的设计目标。
"""

import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cookies as cookie_source  # noqa: E402
from config import DATA_DIR, DB_PATH as _CFG_DB, REPORTS_DIR, ensure_dirs, load as load_cfg  # noqa: E402

DATA = DATA_DIR
REPORTS = REPORTS_DIR
DB_PATH = _CFG_DB
ACTION_CACHE = os.path.join(DATA, "actions.json")

# 全局配置（只读）。缺 config.json 时 config.load 会生成示例并给出提示。
_CFG = load_cfg()

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
UA_APP = "MaiMai/6.6.84 (Android 13; 1080x2340)"

# 已知可用（2026-11 发版），发现失败时兜底。发版后若失效，discover_actions 会重新解析。
ACTION_FALLBACK = {
    "getGossipComments": "7f5849440e01ac777c1a003e30961548d4d46a594a",
    "getGossipLv2Comments": "7f2e5f62b84147f6f1a99645f7afe3015c468d4efd",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
  id INTEGER PRIMARY KEY,
  webcid TEXT NOT NULL,
  name TEXT,
  egid TEXT,
  author TEXT,
  text TEXT,
  posted_at TEXT,
  time_raw TEXT,
  likes INTEGER DEFAULT 0,
  cmts INTEGER DEFAULT 0,
  spreads INTEGER DEFAULT 0,
  ip_loc TEXT,
  first_seen TEXT,
  last_seen TEXT,
  last_cmts INTEGER DEFAULT 0,
  comments_synced_at TEXT,
  comments_synced_cnt INTEGER DEFAULT 0,
  raw TEXT
);
CREATE TABLE IF NOT EXISTS comments (
  id INTEGER PRIMARY KEY,
  gid INTEGER NOT NULL,
  parent_id INTEGER,
  reply_to_cid INTEGER,
  author TEXT,
  text TEXT,
  reply_to TEXT,
  likes INTEGER DEFAULT 0,
  time_str TEXT,
  ip_loc TEXT,
  is_author INTEGER DEFAULT 0,
  first_seen TEXT,
  last_seen TEXT,
  raw TEXT
);
CREATE INDEX IF NOT EXISTS idx_comments_gid ON comments(gid);
CREATE INDEX IF NOT EXISTS idx_comments_parent ON comments(parent_id);
CREATE INDEX IF NOT EXISTS idx_comments_first_seen ON comments(first_seen);
CREATE TABLE IF NOT EXISTS circles (
  webcid TEXT PRIMARY KEY,
  name TEXT,
  first_seen TEXT,
  last_seen TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT,
  finished_at TEXT,
  webcid TEXT,
  posts_seen INTEGER,
  posts_new INTEGER,
  comments_new INTEGER,
  notes TEXT
);
"""


# ---------------------------------------------------------------- helpers
def now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def load_cookies():
    """登录态由 cookies.py 统一获取（config / 环境变量 / 约定路径三选一）。"""
    return cookie_source.load(_CFG)


def make_session():
    cookies = load_cookies()
    s = requests.Session()
    for k, v in cookies.items():
        s.cookies.set(k, v, domain=".maimai.cn")
    s.headers.update(
        {
            "User-Agent": UA,
            "Accept-Language": "zh-CN,zh;q=0.9",
            "x-csrf-token": cookies.get("csrftoken", ""),
        }
    )
    s.cookies_dict = cookies  # type: ignore[attr-defined]
    return s


def connect():
    ensure_dirs()
    os.makedirs(DATA, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(posts)")}
    if "last_cmts" not in cols:
        conn.execute("ALTER TABLE posts ADD COLUMN last_cmts INTEGER DEFAULT 0")
    if "name" not in cols:
        conn.execute("ALTER TABLE posts ADD COLUMN name TEXT")
    ccols = {r["name"] for r in conn.execute("PRAGMA table_info(comments)")}
    if "reply_to_cid" not in ccols:
        conn.execute("ALTER TABLE comments ADD COLUMN reply_to_cid INTEGER")
    if "last_seen" not in ccols:
        conn.execute("ALTER TABLE comments ADD COLUMN last_seen TEXT")
    if "time_guess" not in cols:
        conn.execute("ALTER TABLE posts ADD COLUMN time_guess INTEGER DEFAULT 0")
    # 每日快照：日报要算「跟帖涨了多少」，只看总量看不出变化
    conn.executescript(
        """CREATE TABLE IF NOT EXISTS post_stats (
             gid INTEGER NOT NULL,
             day TEXT NOT NULL,
             likes INTEGER DEFAULT 0,
             cmts INTEGER DEFAULT 0,
             spreads INTEGER DEFAULT 0,
             seen_at TEXT,
             PRIMARY KEY (gid, day)
           );"""
    )
    conn.commit()
    return conn


REL_RE = re.compile(r"^(\d+)\s*(分钟|小时|天|周)前$")


def parse_time_str(value, default=None):
    """把 '刚刚' / '3天前' / '11-12' / '昨天 12:30' 变成 ISO 时间。"""
    if not value:
        return default
    v = str(value).strip()
    now = dt.datetime.now().astimezone()
    if v in ("刚刚", "just now"):
        return now.isoformat(timespec="seconds")
    m = REL_RE.match(v)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {
            "分钟": dt.timedelta(minutes=n),
            "小时": dt.timedelta(hours=n),
            "天": dt.timedelta(days=n),
            "周": dt.timedelta(weeks=n),
        }[unit]
        return (now - delta).isoformat(timespec="seconds")
    m = re.match(r"^昨天\s*(\d{1,2}):(\d{2})$", v)
    if m:
        d = (now - dt.timedelta(days=1)).replace(
            hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0
        )
        return d.isoformat(timespec="seconds")
    # 翻到很久以前的帖时，脉脉会给完整日期（2023-11-27），必须单独处理，
    # 否则会落回 default=今天，把三年前的帖当成今天的新帖。
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", v)
    if m:
        try:
            d = dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), 12, 0, 0)
        except ValueError:
            return default
        return d.astimezone().isoformat(timespec="seconds")
    m = re.match(r"^(\d{1,2})-(\d{1,2})$", v)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        year = now.year
        try:
            d = now.replace(month=month, day=day, hour=12, minute=0, second=0, microsecond=0)
        except ValueError:
            return default
        if d > now + dt.timedelta(days=1):
            d = d.replace(year=year - 1)
        return d.isoformat(timespec="seconds")
    return default


# ---------------------------------------------------------------- server actions
def _chunk_urls(html):
    urls = []
    for m in re.finditer(r'<script[^>]+src="([^"]+)"', html):
        u = m.group(1)
        if "/_next/static/chunks/" in u:
            urls.append(u if u.startswith("http") else "https://s.taou.com" + u)
    return urls


def discover_actions(session, webcid, gid, egid):
    """从 Next.js chunk 里解析 createServerReference 的 action id。"""
    cached = {}
    if os.path.exists(ACTION_CACHE):
        try:
            cached = json.load(open(ACTION_CACHE, encoding="utf-8"))
        except Exception:
            cached = {}
    actions = (cached.get("actions") or {})
    if actions.get("getGossipComments") and actions.get("getGossipLv2Comments"):
        # 校验一次，缓存失效则重找
        if _probe_action(session, webcid, gid, egid, actions["getGossipComments"]):
            return actions

    detail_url = (
        "https://maimai.cn/community/gossip-detail/%d?webcid=%s&gid=%d&egid=%s"
        % (gid, webcid, gid, egid)
    )
    found = {}
    try:
        html = session.get(
            detail_url, timeout=40, headers={"Accept": "text/html,application/xhtml+xml"}
        ).text
        for u in _chunk_urls(html):
            if "gossip-detail" in u and "page-" in u:
                js = session.get(u, timeout=40).text
                for m in re.finditer(
                    r'createServerReference\)\("([0-9a-f]{42})"[^)]{0,120}?"(\w+)"\)', js
                ):
                    found.setdefault(m.group(2), m.group(1))
    except Exception as exc:  # noqa: BLE001
        print("[warn] action discovery failed: %s" % exc, file=sys.stderr)

    actions = dict(ACTION_FALLBACK)
    actions.update({k: v for k, v in found.items() if k in ACTION_FALLBACK})
    try:
        json.dump({"actions": actions, "discovered_at": now_iso()},
                  open(ACTION_CACHE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    except Exception:
        pass
    if found:
        print("  [actions] discovered %d refs from chunk" % len(found))
    return actions


def _probe_action(session, webcid, gid, egid, action):
    try:
        body = call_action(session, webcid, gid, egid, action,
                           [{"gid": gid, "egid": egid, "page": 0}])
    except Exception:
        return False
    return extract_comments(body) is not None


def call_action(session, webcid, gid, egid, action, data):
    url = "https://maimai.cn/community/gossip-detail/%d?webcid=%s&gid=%d&egid=%s" % (
        gid, webcid, gid, egid)
    r = session.post(
        url,
        headers={
            "accept": "text/x-component",
            "content-type": "text/plain;charset=UTF-8",
            "next-action": action,
            "referer": url,
        },
        data=json.dumps(data), timeout=45,
    )
    return r.content.decode("utf-8", "replace")


def parse_flight(text):
    rows = []
    for line in text.splitlines():
        m = re.match(r"^([0-9a-f]+):(.*)$", line)
        if not m or not m.group(2):
            continue
        try:
            rows.append(json.loads(m.group(2)))
        except Exception:
            pass
    return rows


def extract_comments(body):
    for obj in parse_flight(body):
        if isinstance(obj, dict) and isinstance(obj.get("comments"), dict):
            return obj["comments"]
    return None


# ---------------------------------------------------------------- fetch
def fetch_feed(session, webcid, page=0, count=20):
    cookies = getattr(session, "cookies_dict", {})
    csrf = cookies.get("csrftoken", "")
    uid = cookies.get("u", "0")
    referer = "https://maimai.cn/company/gossip_discuss?webcid=" + webcid

    common = {
        "webcid": webcid, "u": uid, "channel": "www", "version": "4.0.0",
        "page": page, "count": count,
    }
    if csrf:
        common["_csrf"] = csrf
    try:
        r = session.get(
            "https://maimai.cn/groundhog/gossip/v3/feed",
            params=common, headers={"Accept": "application/json", "Referer": referer}, timeout=30,
        )
        if r.status_code == 200 and r.text[:1] == "{":
            payload = r.json()
            items = payload.get("data") or []
            if items:
                return items, payload
    except Exception:
        pass

    # 兜底：App 端 open.taou API（用 access_token，不需要 csrf）
    at = cookies.get("access_token", "")
    q = (
        "u=%s&access_token=%s&version=6.6.84&ver_code=android_6684&channel=App&appid=3"
        "&device=Pixel&udid=abc&net=wifi&webcid=%s&page=%d&count=%d"
        % (uid, at, webcid, page, count)
    )
    r = requests.get(
        "https://open.taou.com/maimai/gossip/v3/feed?" + q,
        headers={"User-Agent": UA_APP, "Accept": "application/json"}, timeout=30,
    )
    payload = r.json() if r.text[:1] == "{" else {}
    return payload.get("data") or [], payload


def _collect_replies(session, actions, webcid, gid, egid, top, max_pages, sleep):
    """一级评论 top 的全部回复：内联预览 + 翻页补齐（含折叠在一级里的楼中楼）。"""
    cid = top.get("id")
    n = int(top.get("n") or 0)
    bucket = {}
    for it in (top.get("cmt") or []):
        if it.get("id"):
            bucket[it["id"]] = it

    if n > len(bucket):
        for page in range(max_pages):
            body = call_action(session, webcid, gid, egid, actions["getGossipLv2Comments"],
                               [{"page": page, "cid": cid, "egid": egid}])
            cm = extract_comments(body) or {}
            lst = cm.get("lst") or []
            fresh = 0
            for it in lst:
                i = it.get("id")
                if i and i not in bucket:
                    bucket[i] = it
                    fresh += 1
            if not lst or not cm.get("more"):
                break
            if fresh == 0 and page > 0:
                break
            time.sleep(sleep)

    out = []
    for it in bucket.values():
        it["_parent_cid"] = cid
        out.append(it)
        # 楼中楼里再嵌套的回复，摊平挂在同一条一级评论下
        for sub in (it.get("cmt") or []):
            if sub.get("id") and sub["id"] not in bucket:
                sub["_parent_cid"] = cid
                sub["_reply_to_cid"] = it.get("id")
                out.append(sub)
    return out


def fetch_comments(session, actions, webcid, gid, egid,
                   max_pages=8, max_lv2_pages=6, sleep=0.3, verbose=True):
    """返回 (tops, replies)。replies 里每条带 _parent_cid。"""
    tops, folded = [], 0
    for page in range(max_pages):
        body = call_action(session, webcid, gid, egid, actions["getGossipComments"],
                           [{"gid": gid, "egid": egid, "page": page}])
        cm = extract_comments(body)
        if not cm:
            break
        lst = cm.get("lst") or []
        if not lst:
            break
        tops.extend(lst)
        folded += int(cm.get("folded_cnt") or 0)
        if verbose:
            print("      comments page=%d n=%d more=%s folded=%s" % (
                page, len(lst), cm.get("more"), cm.get("folded_cnt")))
        if not cm.get("more"):
            break
        time.sleep(sleep)

    replies, seen = [], set()
    for c in tops:
        got = _collect_replies(session, actions, webcid, gid, egid, c, max_lv2_pages, sleep)
        for it in got:
            if it.get("id") and it["id"] not in seen:
                seen.add(it["id"])
                replies.append(it)
        time.sleep(sleep)
    return tops, replies, folded


# ---------------------------------------------------------------- db
def _author(item):
    for key in ("author_info", "u"):
        node = item.get(key)
        if isinstance(node, dict) and node.get("name"):
            return str(node["name"])
    for key in ("realname", "name", "company_user_name"):
        val = item.get(key)
        if isinstance(val, str) and val and not val.isdigit():
            return val
    return ""


def text_of(c):
    return (c.get("rt") or c.get("text") or c.get("copy_text") or "").strip()


TIME_COARSE_RE = re.compile(r"^(\d+)\s*(天|周)前$|^\d{4}-\d{1,2}-\d{1,2}$|^\d{1,2}-\d{1,2}$")


def time_precision(raw):
    """0 = 小时级（刚刚 / X分钟前 / X小时前），1 = 天级或更粗。

    第一次看到某帖时，页面上的相对时间最精确（"5小时前" 精确到小时）；
    过两天变成 "2天前"，就只能定到天。所以 posted_at 必须**首次即冻结**，
    否则每次 sync 都用不同的相对时间重算，历史日期会漂移。
    """
    return 1 if TIME_COARSE_RE.match((raw or "").strip()) else 0


def upsert_post(conn, webcid, item, stamp, circle_name=""):
    gid = int(item["id"])
    row = conn.execute(
        "SELECT cmts, last_cmts, posted_at, time_raw FROM posts WHERE id=?", (gid,)
    ).fetchone()
    is_new = row is None
    prev = None
    if row is not None:
        prev = row["last_cmts"] if row["last_cmts"] is not None else row["cmts"]

    raw_time = item.get("time") or ""
    fresh = parse_time_str(raw_time, stamp)
    prec = time_precision(raw_time)
    posted, guess = fresh, prec
    if row is not None and row["posted_at"]:
        # 已有更精确的（小时级）就不被日级覆盖；同级则保留首次值
        if not (time_precision(row["time_raw"]) == 0 and prec == 1):
            posted, guess = row["posted_at"], time_precision(row["time_raw"])

    conn.execute(
        """INSERT INTO posts (id, webcid, name, egid, author, text, posted_at, time_raw, likes, cmts,
                              spreads, ip_loc, first_seen, last_seen, last_cmts, raw, time_guess)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
             name=excluded.name, author=excluded.author, text=excluded.text,
             posted_at=excluded.posted_at, time_raw=excluded.time_raw, likes=excluded.likes,
             cmts=excluded.cmts, spreads=excluded.spreads, ip_loc=excluded.ip_loc,
             last_seen=excluded.last_seen, last_cmts=excluded.last_cmts, raw=excluded.raw,
             time_guess=excluded.time_guess""",
        (
            gid, webcid, circle_name, item.get("egid") or "", _author(item),
            (item.get("text") or "").strip(), posted, item.get("time") or "",
            int(item.get("likes") or 0), int(item.get("cmts") or 0),
            int(item.get("spreads") or 0), item.get("ip_loc") or "",
            stamp, stamp, int(item.get("cmts") or 0), json.dumps(item, ensure_ascii=False),
            guess,
        ),
    )
    return is_new, (int(prev) if prev is not None else None)


def save_comments(conn, gid, tops, replies, stamp):
    merged = {}
    for c in list(tops) + list(replies):
        if c.get("id"):
            merged.setdefault(c["id"], c)
    added = 0
    for cid, c in merged.items():
        parent = c.get("_parent_cid")
        reply_cid = c.get("_reply_to_cid")
        exists = conn.execute("SELECT 1 FROM comments WHERE id=?", (cid,)).fetchone()
        if exists:
            conn.execute(
                """UPDATE comments SET likes=?, text=?, reply_to=?, last_seen=?, raw=?,
                                     parent_id=COALESCE(?, parent_id) WHERE id=?""",
                (int(c.get("likes") or 0), text_of(c), c.get("reply_to_name") or "",
                 stamp, json.dumps(c, ensure_ascii=False), parent, cid),
            )
            continue
        added += 1
        conn.execute(
            """INSERT INTO comments (id, gid, parent_id, reply_to_cid, author, text, reply_to,
                                     likes, time_str, ip_loc, is_author, first_seen, last_seen, raw)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                cid, gid, parent, reply_cid, _author(c), text_of(c),
                c.get("reply_to_name") or "", int(c.get("likes") or 0),
                c.get("time_str") or "", c.get("ip_loc") or "",
                int(c.get("is_author") or 0), stamp, stamp,
                json.dumps(c, ensure_ascii=False),
            ),
        )
    cnt = conn.execute("SELECT COUNT(*) c FROM comments WHERE gid=?", (gid,)).fetchone()["c"]
    conn.execute(
        "UPDATE posts SET comments_synced_at=?, comments_synced_cnt=? WHERE id=?",
        (stamp, cnt, gid),
    )
    return added


# ---------------------------------------------------------------- commands
def cmd_sync(args):
    stamp = now_iso()
    conn = connect()
    session = make_session()
    stats = {"webcid": args.webcid, "posts_seen": 0, "posts_new": 0,
             "comments_new": 0, "gaps": {}, "actions": {}}

    seen, items = set(), []
    for page in range(args.pages):
        page_items, payload = fetch_feed(session, args.webcid, page=page, count=20)
        if not page_items:
            break
        fresh = [i for i in page_items if i.get("id") and int(i["id"]) not in seen]
        for i in fresh:
            seen.add(int(i["id"]))
        items.extend(fresh)
        if len(page_items) < 20:
            break
        if page + 1 < args.pages:
            time.sleep(args.sleep)
    stats["posts_seen"] = len(items)
    if not items:
        print("no posts fetched — cookies expired?")
        return 2

    new_ids, changed_ids = [], []
    for item in items:
        gid = int(item["id"])
        cur_cmts = int(item.get("cmts") or 0)
        is_new, prev_cmts = upsert_post(conn, args.webcid, item, stamp, args.name)
        if is_new:
            stats["posts_new"] += 1
            new_ids.append(gid)
        elif prev_cmts is not None and prev_cmts != cur_cmts:
            changed_ids.append(gid)
    # 当日快照：日报要算「今天涨了多少」，只看总量看不出变化
    today = dt.date.today().isoformat()
    for item in items:
        conn.execute(
            """INSERT INTO post_stats (gid, day, likes, cmts, spreads, seen_at) VALUES (?,?,?,?,?,?)
               ON CONFLICT(gid, day) DO UPDATE SET
                 likes=MAX(post_stats.likes, excluded.likes),
                 cmts=MAX(post_stats.cmts, excluded.cmts),
                 spreads=MAX(post_stats.spreads, excluded.spreads),
                 seen_at=excluded.seen_at""",
            (int(item["id"]), today, int(item.get("likes") or 0),
             int(item.get("cmts") or 0), int(item.get("spreads") or 0), stamp),
        )
    conn.execute(
        """INSERT INTO circles (webcid, name, first_seen, last_seen) VALUES (?,?,?,?)
           ON CONFLICT(webcid) DO UPDATE SET name=excluded.name, last_seen=excluded.last_seen""",
        (args.webcid, args.name, stamp, stamp),
    )
    conn.commit()

    # 拉跟帖：新帖 / 跟帖数有变化的帖 / --force 全量
    candidates = []
    for item in items:
        gid = int(item["id"])
        if not (args.force or gid in new_ids or gid in changed_ids):
            continue
        candidates.append((gid, item.get("egid") or "", int(item.get("cmts") or 0)))

    if candidates:
        actions = discover_actions(session, args.webcid, candidates[0][0], candidates[0][1])
        stats["actions"] = actions
        print("actions: comments=%s lv2=%s" % (
            actions["getGossipComments"][:12], actions["getGossipLv2Comments"][:12]))

    # 默认 max_comment_posts=0 表示「全都抓」，不设上限。
    # 之前默认只抓前 N 条高跟帖，导致库里 418 条帖的跟帖是空的 —— 日报只有标题没讨论。
    if args.max_comment_posts and args.max_comment_posts > 0:
        picked = sorted(candidates, key=lambda x: -x[2])[: args.max_comment_posts]
    else:
        picked = candidates

    for gid, egid, cmts in picked:
        print("  post %s cmts=%s -> fetching replies" % (gid, cmts))
        try:
            tops, replies, folded = fetch_comments(
                session, stats["actions"], args.webcid, gid, egid,
                max_pages=args.max_pages, sleep=args.sleep, verbose=args.verbose)
        except Exception as exc:  # noqa: BLE001
            print("    FAILED: %s" % exc)
            continue
        added = save_comments(conn, gid, tops, replies, stamp)
        stats["comments_new"] += added
        got = len({c.get("id") for c in list(tops) + list(replies) if c.get("id")})
        if got != cmts:
            stats["gaps"][gid] = {"reported": cmts, "fetched": got, "folded": folded}
            print("    top=%d replies=%d stored=%d (post cmts=%d, diff=%+d, folded=%d)" % (
                len(tops), len(replies), got, cmts, got - cmts, folded))
        else:
            print("    top=%d replies=%d stored=%d (matches cmts)" % (
                len(tops), len(replies), got))
        conn.commit()
        time.sleep(args.sleep)

    conn.execute(
        "INSERT INTO runs (started_at, finished_at, webcid, posts_seen, posts_new, comments_new, notes)"
        " VALUES (?,?,?,?,?,?,?)",
        (stamp, now_iso(), args.webcid, stats["posts_seen"], stats["posts_new"],
         stats["comments_new"],
         "picked=%d gaps=%s" % (len(picked), json.dumps(stats["gaps"], ensure_ascii=False))),
    )
    conn.commit()
    conn.close()
    print(json.dumps(stats, ensure_ascii=False))
    return 0


def cmd_backfill(args):
    """补齐历史欠账：库里「跟帖数 > 已抓跟帖数」的帖，逐条补抓。

    和 sync 分开的原因：sync 只处理本轮 feed 里出现过的帖（约 50-100 条），
    而库里积压了 400+ 条老帖从没抓过跟帖。补抓是一次性的大活，
    不该混在日常抓取里，也不该因为 feed 翻不到就永远空着。
    """
    stamp = now_iso()
    conn = connect()
    session = make_session()
    stats = {"picked": 0, "comments_new": 0, "failed": 0, "gaps": {}, "actions": {}}

    where = ["cmts > COALESCE(comments_synced_cnt, -1)"]
    if args.webcid:
        where.append("webcid = ?")
        params = (args.webcid,)
    else:
        params = ()
    order = "cmts DESC" if args.by_comments else "COALESCE(posted_at, first_seen) DESC"
    sql = ("SELECT id, egid, cmts, COALESCE(comments_synced_cnt,-1) AS synced FROM posts "
           "WHERE " + " AND ".join(where) + " ORDER BY " + order)
    rows = conn.execute(sql, params).fetchall()

    # 已有部分跟帖的不值得重抓全部（成本高、增量小），除非 --force
    if not args.force:
        rows = [r for r in rows if r["synced"] <= 0]
    if args.limit and args.limit > 0:
        rows = rows[: args.limit]

    stats["picked"] = len(rows)
    if not rows:
        print("没有需要补抓的帖子（库里跟帖已齐）")
        conn.close()
        return 0
    print("待补抓 %d 条帖，按 %s 排序" % (len(rows), "跟帖数" if args.by_comments else "发帖时间"))

    first = rows[0]
    actions = discover_actions(session, first["id"] and (args.webcid or _CFG["circle"]["webcid"]),
                               first["id"], first["egid"] or "")
    stats["actions"] = actions
    print("actions: comments=%s lv2=%s" % (
        actions["getGossipComments"][:12], actions["getGossipLv2Comments"][:12]))

    for i, r in enumerate(rows, 1):
        gid, egid, cmts = r["id"], r["egid"] or "", r["cmts"]
        webcid = args.webcid or _CFG["circle"]["webcid"]
        print("[%d/%d] post %s cmts=%s" % (i, len(rows), gid, cmts))
        try:
            tops, replies, folded = fetch_comments(
                session, actions, webcid, gid, egid,
                max_pages=args.max_pages, sleep=args.sleep, verbose=False)
        except Exception as exc:  # noqa: BLE001
            stats["failed"] += 1
            print("    FAILED: %s" % exc)
            time.sleep(args.sleep)
            continue
        added = save_comments(conn, gid, tops, replies, stamp)
        stats["comments_new"] += added
        got = len({c.get("id") for c in list(tops) + list(replies) if c.get("id")})
        if got != cmts:
            stats["gaps"][gid] = {"reported": cmts, "fetched": got, "folded": folded}
        print("    stored=%d (post cmts=%d, diff=%+d) +%d new" % (
            got, cmts, got - cmts, added))
        conn.commit()
        time.sleep(args.sleep)

    conn.execute(
        "INSERT INTO runs (started_at, finished_at, webcid, posts_seen, posts_new, comments_new, notes)"
        " VALUES (?,?,?,?,?,?,?)",
        (stamp, now_iso(), args.webcid or "", 0, 0, stats["comments_new"],
         "backfill picked=%d failed=%d gaps=%s" % (
             stats["picked"], stats["failed"],
             json.dumps(stats["gaps"], ensure_ascii=False))),
    )
    conn.commit()
    conn.close()
    print(json.dumps(stats, ensure_ascii=False))
    return 0


def _post_comments(conn, gid):
    rows = conn.execute(
        "SELECT * FROM comments WHERE gid=? ORDER BY (likes) DESC, id ASC", (gid,)
    ).fetchall()
    tops = [r for r in rows if r["parent_id"] is None]
    kids = {}
    for r in rows:
        if r["parent_id"] is not None:
            kids.setdefault(r["parent_id"], []).append(r)
    for lst in kids.values():
        lst.sort(key=lambda r: (r["likes"] or 0), reverse=True)
    return tops, kids, len(rows)


def _fmt_comment(c):
    who = c["author"] or "匿名"
    if c["is_author"]:
        who += "（楼主）"
    rep = "　回复 @%s" % c["reply_to"] if c["reply_to"] else ""
    return "**%s**%s：%s　_(%s · 赞%d)_" % (
        who, rep, (c["text"] or "").replace("\n", " "), c["time_str"] or "", c["likes"] or 0)


def cmd_report(args):
    conn = connect()
    if args.all:
        since = "0000"
        window = "全量"
    else:
        since = (dt.datetime.now().astimezone() - dt.timedelta(hours=args.hours)).isoformat(timespec="seconds")
        window = "近 %d 小时（%s 起）" % (args.hours, since)

    circles = {r["webcid"]: r["name"] for r in conn.execute("SELECT * FROM circles")}
    name = circles.get(args.webcid) or args.webcid
    if args.all:
        posts = conn.execute(
            "SELECT * FROM posts WHERE webcid=? ORDER BY (likes+cmts+spreads) DESC", (args.webcid,)
        ).fetchall()
    else:
        posts = conn.execute(
            "SELECT * FROM posts WHERE webcid=? AND first_seen>=? ORDER BY (likes+cmts+spreads) DESC",
            (args.webcid, since),
        ).fetchall()

    total_posts = conn.execute("SELECT COUNT(*) c FROM posts").fetchone()["c"]
    total_comments = conn.execute("SELECT COUNT(*) c FROM comments").fetchone()["c"]
    new_comments = conn.execute(
        "SELECT COUNT(*) c FROM comments WHERE first_seen>=?", (since,)
    ).fetchone()["c"]

    lines = [
        "# 脉脉同事圈监控 · %s" % name,
        "",
        "- 生成时间：%s" % now_iso(),
        "- 统计窗口：%s" % window,
        "- 库内累计：%d 帖 / %d 条跟帖（一级+回复）" % (total_posts, total_comments),
        "- 本窗口：**新帖 %d** / **新跟帖 %d**" % (len(posts), new_comments),
        "",
        "## 一、热帖速览",
        "",
        "| # | 帖子 | 赞 | 跟帖 | 转发 | 发布 |",
        "|---|------|----|------|------|------|",
    ]
    for i, p in enumerate(posts[:15], 1):
        title = (p["text"] or "").split("\n")[0][:46].replace("|", "/") or "(无正文)"
        lines.append("| %d | %s | %d | %d | %d | %s |" % (
            i, title, p["likes"] or 0, p["cmts"] or 0, p["spreads"] or 0,
            p["time_raw"] or "-"))

    lines += ["", "## 二、帖子与跟帖", ""]
    for i, p in enumerate(posts, 1):
        tops, kids, stored = _post_comments(conn, p["id"])
        lines += [
            "### %d. %s ｜ %s" % (i, (p["text"] or "").split("\n")[0][:70] or "(无正文)",
                                  p["author"] or "匿名"),
            "",
            "- 赞 %d · 跟帖 %d · 转发 %d · %s · IP %s · gid %s" % (
                p["likes"] or 0, p["cmts"] or 0, p["spreads"] or 0,
                p["time_raw"] or "-", p["ip_loc"] or "-", p["id"]),
        ]
        if stored:
            diff = stored - (p["cmts"] or 0)
            lines.append("- 已入库跟帖：%d 条（一级 %d + 回复 %d）%s" % (
                stored, len(tops), stored - len(tops),
                "" if diff == 0 else "，与页面差 %+d 条（折叠/删除）" % diff))
        body = (p["text"] or "").strip()
        lines += ["", "> " + body.replace("\n", "\n> ") if body else "", ""]

        if not stored:
            lines += ["_（该帖跟帖尚未抓取）_", ""]
            continue
        shown = 0
        for t in tops:
            lines.append("- " + _fmt_comment(t))
            for k in kids.get(t["id"], []):
                lines.append("  - └ " + _fmt_comment(k))
                shown += 1
                if shown > 120:
                    break
            shown += 1
            if shown > 120:
                lines.append("- …（跟帖过多，报告截断）")
                break
        lines.append("")

    md = "\n".join(lines).rstrip() + "\n"
    os.makedirs(REPORTS, exist_ok=True)
    tag = "all" if args.all else "h%d" % args.hours
    path = os.path.join(REPORTS, "digest-%s-%s.md" % (
        dt.datetime.now().strftime("%Y%m%d-%H%M"), tag))
    open(path, "w", encoding="utf-8").write(md)
    conn.close()
    print(md)
    print("\n[saved] %s" % path)
    return 0


def cmd_status(args):
    conn = connect()
    rows = conn.execute(
        "SELECT p.webcid, c.name, COUNT(*) posts, MAX(p.last_seen) last,"
        " SUM(p.cmts) cmts, SUM(p.comments_synced_cnt) synced"
        " FROM posts p LEFT JOIN circles c ON c.webcid=p.webcid GROUP BY p.webcid"
    ).fetchall()
    print("circles:")
    for r in rows:
        print("  %s (%s) posts=%d cmts=%d stored=%d last=%s" % (
            r["webcid"], r["name"] or "-", r["posts"], r["cmts"] or 0,
            r["synced"] or 0, r["last"]))
    print("comments: %d" % conn.execute("SELECT COUNT(*) c FROM comments").fetchone()["c"])
    print("top-level: %d" % conn.execute(
        "SELECT COUNT(*) c FROM comments WHERE parent_id IS NULL").fetchone()["c"])
    print("recent runs:")
    for r in conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 6"):
        print("  #%d %s posts=%d new=%d cmts_new=%d %s" % (
            r["id"], r["finished_at"], r["posts_seen"], r["posts_new"], r["comments_new"], r["notes"]))
    conn.close()
    return 0


def main():
    ap = argparse.ArgumentParser(description="脉脉同事圈 watcher")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("sync", help="抓取并入库")
    sp.add_argument("--webcid", default=None, help="同事圈 id；不传则用 config.json")
    sp.add_argument("--name", default="", help="圈名，如 腾讯同事圈")
    sp.add_argument("--pages", type=int, default=30, help="feed 翻几页；0=一直翻到没新帖")
    sp.add_argument("--max-comment-posts", type=int, default=0,
                    help="最多深入抓几条帖的跟帖；0=全部（默认）")
    sp.add_argument("--max-pages", type=int, default=8, help="一级评论最多翻几页")
    sp.add_argument("--sleep", type=float, default=0.3)
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--verbose", action="store_true")
    sp.set_defaults(func=cmd_sync)

    bf = sub.add_parser("backfill", help="补齐历史帖缺失的跟帖")
    bf.add_argument("--webcid", default=None, help="同事圈 id；不传则用 config.json")
    bf.add_argument("--limit", type=int, default=0, help="本次最多补几条；0=不限")
    bf.add_argument("--by-comments", action="store_true", help="按跟帖数从多到少补（默认按时间）")
    bf.add_argument("--max-pages", type=int, default=8)
    bf.add_argument("--sleep", type=float, default=0.3)
    bf.add_argument("--force", action="store_true", help="已抓过部分跟帖的也重抓")
    bf.set_defaults(func=cmd_backfill)

    rp = sub.add_parser("report", help="输出报告")
    rp.add_argument("--webcid", default=None)
    rp.add_argument("--hours", type=int, default=24)
    rp.add_argument("--all", action="store_true", help="全量导出（忽略时间窗）")
    rp.set_defaults(func=cmd_report)

    st = sub.add_parser("status", help="查看库状态")
    st.set_defaults(func=cmd_status)

    args = ap.parse_args()

    # 统一把 --webcid 空值补成 config 里的值。所有子命令都从这里取，
    # 免得每个 cmd_* 各写一遍 `or _CFG[...]`（漏一处就 None 崩溃）。
    if getattr(args, "webcid", None) is None:
        args.webcid = (_CFG.get("circle", {}).get("webcid") or "").strip()
    if not args.webcid and args.cmd != "status":
        raise SystemExit(
            "没有指定同事圈。两种方式：\n"
            "  1) 在 config.json 里填 circle.webcid\n"
            "  2) 命令行加 --webcid <id>\n"
            "webcid 就在同事圈页面 URL 里：.../company/gossip_discuss?webcid=XXXXXXXX"
        )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())