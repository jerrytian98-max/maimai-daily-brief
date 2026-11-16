---
name: maimai-daily-brief
description: 从脉脉「同事圈」抓取帖子和全部跟帖，生成一份只有结论、不带噪声的每日中文简报，并可选推送到企业微信群。当用户提到脉脉、同事圈、职言、内部动态日报、裁员/组织变动舆情监控，或想定时汇总某个内部社区的信息时使用。包含完整的抓取、打分、结论提炼（规则+模型双层）、推送链路，可直接接入定时任务。
---

# 脉脉同事圈每日简报

把「同事圈里一天几千条讨论」压成「一屏能看完、每条都有结论」的日报。

这个 skill 解决的不是「怎么爬」，而是**怎么从噪声里挑出真正值得看的东西**。
默认参数下，一天 20 条新帖 / 500+ 条跟帖，最终只会输出 3 条精选 + 5 条要闻。

---

## 一、核心设计：三个反直觉的决定

### 1. 热度 ≠ 值得关注

最热的帖往往只是情绪共鸣——点赞几百，但读完除了焦虑什么也没得到。
真正有用的帖常常只有个位数赞，因为它是第一人称的、干巴巴的事实陈述：

```
赞 181（最热）  「鹅厂的衰落只是时间问题，根本原因在组织文化……」 ← 全是观点，无新信息
赞  2（沉底）  「股票在 1 月 15 日归属，最早什么时候提离职还能拿到」 ← 能改变你这两周的行为
```

所以打分公式里，**可信度是乘数而不是加数**：

```
value = 可信度 × (1 + 可行动性 + 0.5 × 影响面) + 增速 + 新颖度
```

一条疯传但没来源的帖，乘数低，总分上不去；一条小范围的亲历记录，乘数高，能顶上来。

### 2. 只要「有结论」的

这是用户提的最硬的一条要求：**纯提问、评论区只有「同问 / 蹲 / 插眼」的帖，读完还是不知道答案，不该推**。

所以入选前必须先找到结论，分三级（可信度从高到低）：

| 级别 | 判据 | 说明 |
|---|---|---|
| A 楼主补充 | 楼主自己在评论区给出结果 | 最权威，当事人说的 |
| B 评论区高赞 | 带硬事实（数字/金额/流程）的高赞跟帖 | 群众验证过 |
| C 楼主自述 | 第一人称 + 具体时间/金额，且**不是提问** | 事实陈述 |

找不到就淘汰。**宁可当天只出 1 条，也不拿问句凑够 3 条。**

还会额外拦四类伪结论：问句（含「…吗」「是不是」）、骂人、假设句（「如果…如何…」）、纯感想（「是的，没错，重要的是…」）。

### 3. 规则负责筛，模型负责写

规则能判断「这条帖有答案」，但剪出来常是半句话：

```
规则版：  「37是因为股票太多，所以压低了现金比例」   ← 37 是什么的比例？
模型版：  「WXG年终奖现金股票比例有 5:5、4:6、3:7、3:1 多种说法」  ← 自洽
```

所以最后还有一层：把候选帖（正文 + 高赞跟帖摘录）批量送给模型，让它写一句
「谁、因为什么、结论是什么」都齐整的话。模型有**否决权**——它判定「材料里没有明确答案」就填 null，这条帖直接淘汰。

---

## 二、快速开始

### 步骤 1：装依赖

```bash
pip install requests
# 仅「从 Chrome 导出 cookie」需要：
pip install pywin32 cryptography
```

### 步骤 2：拿同事圈 id

浏览器打开公司的同事圈页面，URL 长这样：

```
https://maimai.cn/company/gossip_discuss?webcid=jYZTTwkX
                                          ^^^^^^^^ 这串就是 webcid
```

### 步骤 3：登录态（三选一）

脉脉接口都要登录态。**cookie 等同账号密码，任何情况下都不要提交到版本库。**

**方式 A：从本机 Chrome 导出（推荐，最省事）**

```bash
python scripts/chrome_cookies.py --list           # 先看有哪些 profile
python scripts/chrome_cookies.py "Default"        # 导出到 cookies.json
```

要求 Chrome 里已登录脉脉，并且**打开过桌面版页面**（移动版没有 csrftoken）。
原理是 DPAPI 解密本机 Chrome 的 cookie 库，不需要管理员权限，也不需要浏览器开着。

**方式 B：环境变量**

```bash
export MAIMAI_COOKIES='{"u":"123456","access_token":"...","csrftoken":"..."}'
```

**方式 C：手工放文件**

把 cookie JSON 存成项目根目录的 `cookies.json`。

> 判断标准：导出的 cookie 里至少要有 `u` 和 `access_token`，
> 有 `csrftoken` 才能走 web 接口（没有的话会走 App 网关兜底）。

### 步骤 4：配置

复制 `config.example.json` 为 `config.json`，填这几项：

```jsonc
{
  "circle": {
    "webcid": "你的同事圈 id",
    "name": "XX同事圈"          // 只用于报告落款
  },
  "llm": {
    "enabled": true,
    "base_url": "https://你的网关/v1",   // 任何 OpenAI Chat Completions 兼容端点
    "model": "你的模型名",
    "api_key_env": "MAIMAI_LLM_API_KEY", // 从哪个环境变量读密钥
    "reasoning_effort": "no_think"       // 思考型模型务必用这个，见下方「坑」
  },
  "push": {
    "enabled": true,
    "webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=你的key"
  }
}
```

### 步骤 5：跑一次

```bash
python scripts/run_daily.py            # 只出报，看看效果
python scripts/run_daily.py --push     # 出报 + 推企业微信
```

输出会同时打到终端、存成 `reports/daily-YYYYMMDD.md`。

---

## 三、日常用法

```bash
# 每天手动跑（滚动 24 小时）
python scripts/run_daily.py --push

# 补出某一天（自然日 0:00-24:00）
python scripts/run_daily.py --date 2026-01-01 --write

# 排查用：附加「为什么排前面」的依据
python scripts/run_daily.py --meta

# 模型网关挂了：退回纯规则版
python scripts/run_daily.py --no-llm
```

### 接入定时任务

推荐 **每天固定时间跑**，窗口自然是「昨天这个点 → 今天这个点」。

- **DSH**：用 `schedule_create`，`RRULE:FREQ=DAILY`，prompt 里写
  `python scripts/run_daily.py --push`
- **Linux cron**：`0 22 * * * cd /path/to/maimai-daily-brief && python3 scripts/run_daily.py --push`
- **Windows 任务计划**：动作填 `python.exe`，参数填 `scripts\run_daily.py --push`，起始于填项目目录

> 为什么强调固定时间：增量信号（今天涨了多少讨论）需要「昨天收盘 vs 今天收盘」两份快照。
> 每天同一时间跑，才能算准。第一次跑只有一份快照，脚本会写「增量暂缺」而不是写 0。

---

## 四、输出长什么样

```markdown
# 同事圈日报 · 11-15 22:00→11-16 22:00

新帖 23 ｜ 跟帖 572 ｜ 主线：赔偿年终(6) · 舆情风险(2)

## 今日精选

**1. WXG年终奖现金股票比例有5:5、4:6、3:7、3:1多种说法** [原帖](https://maimai.cn/...)

**2. 今年年底五星90%安全，部门合并裁撤会有影响** [原帖](https://maimai.cn/...)
　背景：年中或年底拿到五星90%是安全的……

**3. IEG公线某部打五折，已沟通完毕** [原帖](https://maimai.cn/...)

**要闻**
- [行动] 离职流程最后日期可写在1月15之后，极速交接1天也可按该日期离职　[原帖](…)
- [动向] 年底广进重点对象是各工作室中台、IEG GLOBAL、IEG 技术公线　[原帖](…)
```

几个刻意的设计：

- **标题是结论，不是提问** —— 一屏读下来就是答案，不用点进去才知道讲什么
- **链接跟在正文行末** —— 不单独占行，3 条精选不会因此多出 3 个空行
- **不显示排序依据** —— 「评论区 44 赞 / 今日 +17 评 / 已略过 41 条」是给写报告的人看的，
  读者只想知道发生了什么。要看就加 `--meta`
- **背景和原问分开标** —— 帖子本身是提问时标「原问」，避免读者把它当成结论的一部分

---

## 五、踩过的坑（都是实测，不是猜想）

### 抓取层

**① 帖子列表和评论走两套完全不同的接口**

| 数据 | 接口 |
|---|---|
| 帖子列表 | `GET /groundhog/gossip/v3/feed?webcid=..&page=N&_csrf=..` |
| 一级评论 | `POST /community/gossip-detail/<gid>`，header `next-action: <getGossipComments>` |
| 楼中楼 | 同上，`next-action: <getGossipLv2Comments>` |

**② `/web/gossip-detail?gid=..` 会被 WAF 拦（418）**，必须用
`/community/gossip-detail/<gid>?webcid=..&gid=..&egid=..` —— 而且 **egid 不能省**，
省了就是 404。这就是为什么库里要单独存 egid 字段。

**③ server action id 会随前端发版变化**（一长串 42 位十六进制）。
脚本会从 Next.js chunk 里用 `createServerReference("...")` 正则重新解析并缓存到
`data/actions.json`，发版后不需要改代码。也有硬编码兜底，双保险。

**④ 相对时间会把三年前的帖算成今天**

脉脉列表里写「3天前」「11-12」，翻到很久以前会变成完整日期「2023-11-27」。
**如果只解析相对时间，完整日期会落回 default（今天），把老帖当新帖。**
所以 `parse_time_str` 必须单独处理 `YYYY-MM-DD`。

**⑤ `posted_at` 必须首次即冻结**

第一次看到某帖时页面写「5小时前」（精确到小时），过两天变成「2天前」（只能定到天）。
每次 sync 都重算的话，历史日期会**漂移**。所以 upsert 时如果已有更精确的记录，就不被粗粒度覆盖。

### 打分层

**⑥ 别把「职能」当部门**

早期版本用 `职能` 匹配「公线/中台」，结果一句「提离**职能**把」被误判成点名了职能部门。
改成 `职能线|职能部门` 才准。这类过度匹配会静默污染整个分类，很难发现。

**⑦ 玩梗要单独拦**

「年终加 **200 块**」「那就都裁了吧」——规则和模型都可能当真。
`is_joke()` 专门拦「≤1000 的小额金额」「口号式短句」。

**⑧ 比例会被写丢冒号**

同事圈里有人把 `4:6` 写成 `46`、`3:7` 写成 `37`，模型照抄后读者会看成「四十六」。
`normalize_ratios()` 只在**比例语境**（出现「比例/股票/现金/期权」）里补冒号，
并且排除「股票占 46」「200 块」「2026 年」这类误伤。

### 模型层

**⑨ 思考型模型的 max_tokens 陷阱（最容易踩）**

第一次接入时 `max_tokens=4000`，返回的 `content` 是**空字符串** ——
因为 reasoning 阶段先吃光了预算，`finish_reason=length`。

修法两条，都要做：
- `max_tokens` 给足（比如 16000）
- `reasoning_effort` 设成 `no_think`

实测 `no_think` 下 20 秒内返回，而且质量更好（不纠结，直接给结论）；
`low`/`high` 在高负载时容易超过 180 秒超时。

**⑩ 模型是增强，不是依赖**

网关挂了、key 过期、超时 —— 日报必须照常出。所有异常都退回规则版结论，
只打一行警告：

```
[summarize] 调用失败，回退规则版：HTTP Error 401: Unauthorized
```

这一层是硬要求：**不能因为模型挂了当天就没日报。**

### 推送层

**⑪ 企业微信 markdown 上限 4096 字节**

超出会被静默截断。统一压到 3900（留余量），并且按字节截而不是按字符
（中文一个字符 3 字节，按字符截会超）。

**⑫ 本机有系统代理时 qyapi 连不上**

`urllib` 默认走环境代理，企业微信 API 走代理会失败。
显式 `build_opener(ProxyHandler({}))` 空代理直连才通。

---

## 六、文件说明

```
maimai-daily-brief/
├── SKILL.md                  # 本文件
├── README.md                 # 面向 GitHub 的说明
├── config.example.json       # 配置模板
├── requirements.txt
├── .gitignore                # 屏蔽 cookie / config / data
├── scripts/
│   ├── config.py             # 配置加载 + 路径推导
│   ├── cookies.py            # 登录态三种来源
│   ├── chrome_cookies.py     # 从 Chrome 导出 cookie
│   ├── maimai_client.py      # 抓取：feed / 评论 / 楼中楼 / 增量
│   ├── brief.py              # 打分 + 结论识别 + 渲染
│   ├── summarize.py          # 模型改写（可选，带兜底）
│   ├── push_wecom.py         # 企业微信推送
│   └── run_daily.py          # 一键：抓取→出报→推送
└── docs/
    └── architecture.md       # 接口细节与数据模型
```

---

## 七、使用边界（请务必读完）

这是给**已登录用户读自己本来就能看到的内容**用的低量工具。

- ✅ 一个人看自己公司的同事圈，每天跑一次
- ✅ 用默认的请求间隔（0.3 秒），不要改成并发高频
- ❌ 不要用来批量采集他人信息
- ❌ 不要把抓到的内容再分发、二次售卖
- ❌ 不要绕过任何访问控制

**cookie 和 webhook 都等同密码**，泄露的后果是账号被盗 / 群被任意发消息。
`.gitignore` 已经屏蔽了 `cookies.json`、`config.json`、`data/`，但请自己也确认一遍。

内容合规由使用者负责：同事圈里的发言是个人观点，不是公司公告，转述时注意标注来源性质。
