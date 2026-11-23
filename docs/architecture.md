# 接口与数据模型

记录实测到的接口形态和踩过的坑，方便以后接口变化时快速定位。

## 一、抓取链路

### 1. 帖子列表

```
GET https://maimai.cn/groundhog/gossip/v3/feed
    ?webcid=<圈子id>&u=<uid>&channel=www&version=4.0.0&page=<N>&count=20&_csrf=<csrf>
```

- 每页**最多返回 10 条**，传 `count=100` 也只给 10 条 —— 想多抓只能翻页
- 返回 `{data:[...], total, count, remain, new, ...}`
- 帖子字段里 `id`（整数，即 gid）、`egid`（32 位十六进制）、`time`（相对时间字符串）、
  `likes` / `cmts` / `spreads` 都是我们要的

**兜底**：web 接口挂了（cookie 缺 csrf、触发风控）时走 App 网关：

```
GET https://open.taou.com/maimai/gossip/v3/feed
    ?u=<uid>&access_token=<token>&version=..&webcid=..&page=N&count=20
```

不需要 csrf，但要 `access_token`。返回结构一致。

### 2. 一级评论 / 楼中楼

脉脉的详情页是 **Next.js App Router**，评论走 **Server Action**，不是普通 REST：

```
POST https://maimai.cn/community/gossip-detail/<gid>?webcid=..&gid=..&egid=..
Headers:
  accept: text/x-component
  content-type: text/plain;charset=UTF-8
  next-action: <42 位十六进制的 action id>
  referer: <同一个 URL>
Body:
  一级评论  [{"gid":123,"egid":"...","page":0}]
  楼中楼    [{"page":0,"cid":<一级评论id>,"egid":"..."}]
```

返回是 **RSC flight 格式**（每行形如 `1:{...}`），需要按行解析 JSON。
评论在返回体里的 `comments` 键下：

```jsonc
{"comments": {
   "lst": [ {id, rt, u:{name,...}, likes, n, time_str, is_author, cmt:[...]} ],
   "more": 1,        // 还有下一页
   "folded_cnt": 1,  // 被折叠的条数（接口不返回，抓不到）
   "page": 0
}}
```

- `rt` 是评论正文（不是 `text`）
- `n` 是该一级评论下的**总回复数**（含楼中楼），`cmt` 只是内联预览（通常 2 条）
- 所以要拿到全部回复，必须按 `n > len(cmt)` 判断后翻 `getGossipLv2Comments`

### 3. action id 会漂移

两个关键 action：

| 名字 | 用途 |
|---|---|
| `getGossipComments` | 一级评论分页 |
| `getGossipLv2Comments` | 楼中楼分页 |

它们是前端打包时生成的 42 位十六进制串，**前端发版就会变**。

自动发现方式：拉详情页 HTML → 找 `gossip-detail` 相关的 JS chunk →
正则 `createServerReference\("([0-9a-f]{42})"[^)]{0,120}?"(\w+)"\)`。

发现结果缓存到 `data/actions.json`，用之前会先拿一条帖试跑校验，失效才重新发现。

---

## 二、链接格式（必须带 egid）

实测三种写法：

| 写法 | 结果 |
|---|---|
| `/community/gossip-detail/<gid>?webcid=..&gid=..&egid=..` | ✅ 200，含正文 |
| `/community/gossip-detail/<gid>?webcid=..` | ❌ 404 |
| `/web/gossip-detail?gid=..` | ❌ 404（老路径，且会被 WAF 拦 418） |

所以 `posts` 表必须单独存 `egid`，拼链接时不能省。

---

## 三、时间处理

这是最容易出静默错误的地方。

### 相对时间的三种形态

| 形态 | 例子 | 精度 | 处理 |
|---|---|---|---|
| 极近 | `刚刚`、`1小时内` | 分钟级 | 用当前时间 |
| 相对 | `5小时前`、`3天前`、`2周前` | 小时/天级 | 当前时间减去 |
| 月日 | `11-12` | 天级（跨年需推断） | 当年，若晚于今天则算去年 |
| 完整日期 | `2023-11-27` | 天级 | 直接解析 |

**坑**：翻到很旧的帖时，脉脉给的是完整日期。如果解析函数只处理相对时间和月日，
完整日期会落回「default = 当前时间」，导致**三年前的帖被当成今天的新帖**，
一路混进日报。所以 `YYYY-MM-DD` 必须单独 `if` 分支。

### posted_at 首次即冻结

同一条帖：

- 第一天看到：`5小时前` → 精确到小时
- 第三天看到：`2天前` → 只能定到天

如果每次 sync 都重算，**历史日期会漂移**。规则：

```python
if 已有记录的时间精度 == 小时级 and 新记录精度 == 天级:
    保留旧值          # 不被粗粒度覆盖
else:
    保留旧值          # 同级也保留首次的，因为首次最接近真实时刻
```

用 `time_guess` 字段标记精度（0=小时级，1=天级或更粗）。

---

## 四、数据模型

```sql
posts (
  id INTEGER PRIMARY KEY,   -- gid
  webcid TEXT,              -- 所属圈子
  name TEXT,                -- 圈名（冗余，方便多圈子）
  egid TEXT,                -- 32 位串，拼链接必需
  author TEXT, text TEXT,
  posted_at TEXT,           -- ISO 时间，首次冻结
  time_raw TEXT,            -- 原始字符串，保留用于重新解析
  likes INTEGER, cmts INTEGER, spreads INTEGER,
  ip_loc TEXT,
  first_seen TEXT, last_seen TEXT,
  last_cmts INTEGER,        -- 上一次见到的跟帖数，用于判断「有没有新讨论」
  comments_synced_at TEXT,
  comments_synced_cnt INTEGER,  -- 已抓到的跟帖条数
  raw TEXT,                 -- 原始 JSON，便于日后补字段
  time_guess INTEGER        -- 时间精度标记
)

comments (
  id INTEGER PRIMARY KEY,   -- 评论 id
  gid INTEGER,              -- 所属帖
  parent_id INTEGER,        -- NULL = 一级评论；否则指向一级评论
  reply_to_cid INTEGER,     -- 楼中楼里回复的那条评论
  author TEXT, text TEXT, reply_to TEXT,
  likes INTEGER, time_str TEXT, ip_loc TEXT,
  is_author INTEGER,        -- 是否楼主
  first_seen TEXT, last_seen TEXT,
  raw TEXT
)

post_stats (               -- 每日快照，用于算增量
  gid INTEGER, day TEXT,
  likes INTEGER, cmts INTEGER, spreads INTEGER,
  seen_at TEXT,
  PRIMARY KEY (gid, day)
)

circle / runs             -- 圈子元信息 / 每次运行的统计
```

### 为什么要有 post_stats

「今天这条帖涨了多少讨论」不能从 `comments` 表现算（那是全量），
也不能从 `posts.cmts` 看（那只是当前值）。必须有两个时间点的快照相减。

**没有前一天快照时必须返回「暂缺」而不是 0** —— 写 0 会让人误以为「今天没人讨论」。
代码里用 `None` 表示，渲染时显示「—」。

---

## 五、打分公式

```python
cred = 1.0
if 第一人称:      cred += 2.0    # 最强信号
if 有来源转述:    cred += 0.4
if 点名组织:      cred += 0.7
if 同主题≥3帖:    cred += 0.8
cred = min(cred, 5.0)            # 封顶，避免单条帖刷满

act = 0
if 含时间点:  act += 1.6
if 涉及钱:    act += 1.3
if 涉及流程:  act += 1.2
if 点名组织:  act += 0.7
if 含比例:    act += 0.6

scope = 1.0 + 0.9 * 组织数，全公司级 +1.5

value = cred * (1 + act + 0.5 * scope) + min(增速/40, 3) + 0.8 * 新颖度
```

关键在 `cred` 是**乘数**：低可信度的帖即使 act 高（比如含糊其辞地提了钱），
总分也上不去。

---

## 六、常见故障对照

| 现象 | 原因 | 处理 |
|---|---|---|
| 抓取返回空 | cookie 过期 | 重新导出 |
| **浏览器能看、脚本一直 20001** | **磁盘 cookie 滞后于内存态** | **跑 `ensure_login.py`，它会开浏览器取内存 cookie** |
| 抓取 418 | 用了 `/web/gossip-detail` 老路径 | 换 `/community/...` 且带 egid |
| 评论空但帖子有 `cmts` | action id 变了 | 删 `data/actions.json` 重启，会重新发现 |
| 老帖混进今天 | 时间解析缺 `YYYY-MM-DD` 分支 | 见「三、时间处理」 |
| 日报全是问句 | 没启用结论过滤 | 检查 `_concl` 是否为空 |
| 模型返回空串 | reasoning 吃光 token | `max_tokens` 加大 + `reasoning_effort: no_think` |
| 推送超时/失败 | 系统代理 | 代码里已用空 ProxyHandler 直连 |
| 推送成功但内容被截 | 超过 4096 字节 | 已在 push 层压到 3900 |

---

## 七、登录态为什么会「假失效」

这是最容易误判的一类故障，单独说明。

### 三份状态

| 位置 | 内容 | 是否权威 |
|---|---|---|
| 服务端 | 当前有效的 token | ✅ 最终标准 |
| 浏览器内存 | 页面实际在用的 token | ✅ 最接近真相 |
| 浏览器磁盘库 | 上次刷盘时的快照 | ❌ 可能滞后几小时到几天 |

Chromium 不是每次 cookie 变更都立刻写盘。所以会出现：

```
你打开浏览器 → 一切正常（用内存里的新 token）
脚本读磁盘   → 拿到旧 token → 20001
```

### 判别方法

```bash
# 1. 磁盘上的 token 创建时间
python -c "..."   # 看 creation_utc

# 2. 如果创建时间很久远，但浏览器里明明能用 → 就是滞后
```

### 自愈流程

```
预检（count=1）
  ├─ 通过 → 结束（0 成本）
  └─ 失败 → 从磁盘重导 → 再验
              ├─ 通过 → 结束
              └─ 失败 → 复制 profile 开浏览器 → 取 ctx.cookies()（内存态）
                          ├─ 通过 → 写盘，结束
                          └─ 失败 → 真失效，告警要人重新登录
```

只有最后一档才需要人工介入。前两档都是自动的。
