# tg-thumb-fixer 🖼️

> 自动修复 Telegram 频道里 fixvx / fixupx 推文转发帖「没有缩略图」的问题 —— 从 X 拉原图，把帖文改写成「图片 + 原文」

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![Dependencies](https://img.shields.io/badge/dependencies-0-brightgreen.svg)](#-功能特性)
[![Bot API](https://img.shields.io/badge/Telegram-Bot%20API-26A5E4.svg)](https://core.telegram.org/bots/api)
[![CI](https://github.com/LenKiMo/tg-thumb-fixer/actions/workflows/ci.yml/badge.svg)](https://github.com/LenKiMo/tg-thumb-fixer/actions/workflows/ci.yml)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](https://github.com/LenKiMo/tg-thumb-fixer)

把带 `fixvx.com` / `fixupx.com` / `fxtwitter.com` / `vxtwitter.com` 链接的频道帖文**自动补成「图片 + 原文字」**：脚本从 X 拉真正的原图（`?name=orig`），用 Telegram Bot API 改写那条帖文，多图则重发成相册。

零第三方依赖（只用 Python 标准库），单文件，跑在任意能访问 Telegram 与 X 的机器上（cron / systemd timer 都行）。

## 目录

- [问题背景](#-问题背景为什么会缺图)
- [功能特性](#-功能特性)
- [工作原理](#-工作原理)
- [快速开始](#-快速开始)
- [配置项](#-配置项)
- [部署：对机器的要求](#-部署对机器的要求)
- [定时调度](#-定时调度)
- [Cloudflare Workers 的限制](#-cloudflare-workers-的限制本项目的未实施改造)
- [隐私与安全](#-隐私与安全)
- [常见问题](#-常见问题)
- [项目结构](#-项目结构)
- [AIGC 声明](#-aigc-声明)
- [许可证](#-许可证)

## 🧩 问题背景：为什么会缺图

`fixvx` / `fixupx` 这类服务生成的 `og:image` 指向 X 原图（`pbs.twimg.com/...?name=orig`）。Telegram 服务端抓取链接预览时，对图片体积有硬限，**实测约 1.4MB 以上一律抓不到**：

| 原图体积 | 0.82MB | 0.98MB | 1.33MB | 1.52MB | 2.29MB | 2.33MB | 2.59MB |
|---|---|---|---|---|---|---|---|
| Telegram 抓取 | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ |

抓取失败时帖文只剩「站点名 + 标题 + 描述」的文字预览，**没有缩略图，且 Telegram 不会重试** —— 于是美术图类频道里，凡是原图偏大的转发帖就永久缺图。这与镜像域名无关（`fixvx` 与 `fixupx` 同样中招），纯粹是原图太大。

## ✨ 功能特性

- **零依赖**：仅用 Python 标准库（`urllib` / `json` / `re` / `sqlite` 无关），无 `requirements.txt`
- **免账号检测**：不需要任何 Telegram 账号，靠 `t.me/s/<频道>` 的公开预览页判断「这条帖文到底有没有缩略图」
- **免账号拉原图**：走 `api.fxtwitter.com` 解析推文，取 `?name=orig` 原图，不需要 X 账号或 API Key
- **上传字节而非 URL**：`editMessageMedia` 传 `attach://` 上传文件，绕开 Telegram 抓不到图的问题
- **多图三档策略**：`album`（删原帖重发原图相册，保真）／`mosaic`（拼成一张整图原地替换，静默）／`first`（只取第一张）
- **自动降级**：相册失败（缺权限 / 超体积 / 下载不足）自动退回拼图原地改，绝不让帖文没人管
- **链接归一**：补图时把 `fixupx/fixvx` 等镜像域改写成官方域（`x.com` / `twitter.com`），只动 host，保留路径
- **增量轮询**：翻页到上一轮位置就停，静默频道每轮只抓 1 页（≈105KB），不白烧流量
- **幂等**：`state.json` 记录处理过的 message_id，重复运行不会二次改写
- **叠加保护**：锁文件 + 陈旧锁接管，避免上游卡顿时 cron 任务互相叠加
- **常态静默**：只在出错时执行告警命令（可接你自己的通知脚本）

## 🔍 工作原理

1. **扫描**：拉 `https://t.me/s/<频道>?before=<id>`，解析每条 `data-post="频道/ID"` 的帖文块：
   - 带 `link_preview_image` / `link_preview_right_image` / `link_preview_video` → 有缩略图，跳过；
   - 只有 `link_preview_site_name/_title/_description` → **缺图，列入待修**；
   - 帖文自带图/视频、或纯文字无链接 → 不动。
2. **取原图**：`https://api.fxtwitter.com/<user>/status/<id>` → `media.photos[].url`（强制 `name=orig`）；视频取码率最高的 `.mp4`（或封面图）。
3. **改写**：
   - 单图 / 视频 → `editMessageMedia` 上传文件，帖文变成「图片 + 原文字」；
   - 多图 → `album` 模式 `sendMediaGroup` 重发**逐张原图**相册，再删除原帖；`mosaic` 模式则用现成拼图原地替换。
4. **收尾**：`state.json` 记账（含 `pending_delete` 兜底补删），出问题才会触发告警。

实测要点（供判断是否适用你的场景）：

- **编辑他人帖文**：机器人在频道里只要有「编辑消息」权限，就能编辑**别的账号 / 别的 inline bot 发的**帖文，不必是原作者。
- **相册必须删原帖重发**：Bot API 的 `editMessageMedia` 只接受**单个** `InputMedia`，无法给已有消息插多图；想要真正的多图相册只能 `sendMediaGroup`（新消息）+ 删除原帖，副作用是**订阅者会收到新消息通知**、原帖的位置/评论会丢。
- **分辨率**：Telegram 图片会把最长边压到 **2560**；要绝对无损只能发 `document`（显示为文件卡片）。
- **相册只在第一条挂 caption**（Telegram 规则），因此正文 > 1024 字的帖文无法转成图片消息，会被跳过并记日志。

## 🚀 快速开始

### 前置

- Python **3.10+**（用到 `X | None` 类型写法）
- 一个 Bot：找 [@BotFather](https://t.me/BotFather) 用 `/newbot` 创建，拿 token
- 目标频道**是公开频道**（`t.me/s/<频道>` 可访问，脚本靠它检测）
- 把机器人加进频道并授予权限（见下）

### 1. 获取代码与配置

```bash
git clone https://github.com/LenKiMo/tg-thumb-fixer.git
cd tg-thumb-fixer

cp config.example.json config.json     # Windows: copy config.example.json config.json
cp .env.example .env                   # 填 TELEGRAM_BOT_TOKEN=...
# 编辑 config.json，把 channels 改成你的频道用户名（不带 @）
```

### 2. 频道权限

| 你想要的效果 | 机器人需要的权限 |
|---|---|
| 单图 / 视频原地补图（静默，不发通知） | 「编辑消息」 |
| 多图重发相册（`multi_media: "album"`） | 「编辑消息」+「发布消息」+「删除消息」 |

> album 模式缺少后两项权限时会自动降级为拼图原地编辑（不会报错中断）。

### 3. 运行

```bash
# 先干跑看看会改哪些帖（不发任何请求到 Telegram）
python tg_thumb_fixer.py --config config.json --dry-run --backfill 60

# 实跑一次（修完就退出，适合 cron）
python tg_thumb_fixer.py --config config.json --backfill 60

# 常驻轮询（每 interval 秒一轮，适合 systemd）
python tg_thumb_fixer.py --config config.json --watch
```

常用参数：

| 参数 | 说明 |
|---|---|
| `--dry-run` | 只打印将要做什么，不修改 |
| `--backfill N` | 扫描最近 N 条帖文 |
| `--channels a b` | 临时覆盖配置里的频道列表 |
| `--full` | 强制深度扫描（默认增量：翻到上轮位置即停） |
| `--watch` / `--interval N` | 常驻轮询 / 覆盖轮询间隔 |
| `--no-lock` | 忽略叠加保护（调试用） |
| `--fix-one CHAT_ID MESSAGE_ID TEXT` | 手工修一条（调试用） |

## ⚙️ 配置项

| 键 | 默认 | 说明 |
|---|---|---|
| `channels` | `["your_channel"]` | 要监视的公开频道用户名列表（不带 `@`） |
| `interval` | `300` | 常驻模式轮询间隔（秒） |
| `backfill` | `300` | 每轮扫描最近多少条帖文 |
| `photo_mode` | `photo` | `photo`（Telegram 图片，最长边 2560）／`document`（原图文件，无损） |
| `multi_media` | `album` | `album`（删原帖重发相册）／`mosaic`（拼图原地改）／`first`（只第一张） |
| `incremental` | `true` | 增量扫描：翻到上轮位置就停（`--full` 可绕过） |
| `video_mode` | `video` | `video`（附 mp4，>50MB 退回封面）／`poster`（只附封面） |
| `skip_text_over_caption` | `true` | 正文 > 1024 字（caption 上限）时跳过，否则截断 |
| `official_links` | `true` | 补图时把 `fixupx/fixvx` 等镜像域改成官方域 |
| `official_domain` | `null` | 指定则所有镜像域统一改成它（例如 `"x.com"`） |
| `normalize_fixed_links` | `true` | 每轮顺带把**本工具修过的帖**里残留的镜像域链接改成官方域（幂等） |
| `normalize_scope` | `fixed` | `fixed`（只改本工具修过的）／`all`（频道里所有带镜像链接的图/视频帖） |
| `state_file` / `log_file` / `lock_file` | 同目录 | **服务器部署请写绝对路径**（cron 的工作目录不是项目目录，相对路径会把状态写到别处） |
| `alert_cmd` | `null` | 出问题时执行的命令，例如 `["/path/to/notify.sh"]`，常态静默 |
| `dry_run` | `false` | 全局干跑开关 |

## 🖥️ 部署：对机器的要求

这个 bot 本身很轻（一轮扫描 0.2s CPU、约 26MB 内存），**真正的门槛是网络可达性** —— 它必须能直连以下地址：

| 用途 | 目标 | 不能访问会怎样 |
|---|---|---|
| 检测帖文有没有缩略图 | `https://t.me/s/<频道>` | 检测不到任何帖文（日志会报「读不到任何帖文」） |
| 解析推文取原图 | `https://api.fxtwitter.com` | 无法取原图，待修帖全部跳过 |
| 下载原图 / 拼图 | `https://pbs.twimg.com`、`https://mosaic.fxtwitter.com` | 下载失败，跳过 |
| 改写帖文 | `https://api.telegram.org` | 机器人不可用 |

要点：

- **一台能直接连接 X 与 Telegram 服务器的 VPS 是最省事的形态**（上述域名全部可达）。若所在网络无法直连这些域名，需要自行解决出口（系统代理 / 路由），**建议在系统层做，脚本内部不做任何代理配置**，以免把网络策略混进业务代码。
- 机器不需要公网入站端口：本脚本**只做出站请求**，不需要 webhook、不需要开放端口。
- 只需要一个 `cron` 或 `systemd timer`；不需要数据库、不需要 Docker（有也行）。
- 时间：脚本只用到本地时间打印日志，时区无关紧要。

### 定时调度

cron 示例（每 5 分钟一轮，`deploy/cron.example`）：

```cron
*/5 * * * * /usr/bin/python3 /opt/tg-thumb-fixer/tg_thumb_fixer.py --config /opt/tg-thumb-fixer/config.json --backfill 60 >> /opt/tg-thumb-fixer/run.log 2>&1
```

systemd 示例（`deploy/tg-thumb-fixer.service` + `deploy/tg-thumb-fixer.timer`）：

```bash
sudo cp deploy/tg-thumb-fixer.service deploy/tg-thumb-fixer.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tg-thumb-fixer.timer
```

> 用 systemd 常驻时也可以改成 `--watch` 的 oneshot（`deploy/tg-thumb-fixer-watch.service`），省去 timer 调度。

## ☁️ Cloudflare Workers 的限制（本项目的未实施改造）

**本项目目前没有 Workers 版本**：现状是「Python 单文件 + 定时任务」，需要一台常驻机器。若想搬到 Cloudflare Workers 免费版，以下是按官方文档与实测整理的约束，以及**尚未实施**的改造点：

| 限制（Workers 免费版） | 数值 | 对本项目的影响 |
|---|---|---|
| 每日请求数 | 100,000 / 天（入站；cron 每 5 分钟 = 288/天） | 够用；但**若把轮询 fan-out 成子调用，每次子调用都计入** |
| 每次调用 CPU 时间 | **10 ms** | 单页解析在 V8 上实测 0.24ms，所以一次调用约能处理几十页；**频道一多就必须拆成多个调用**，否则超限即 1102 错误 |
| 单次调用内的子请求 | 50 | 抓 t.me + 抓 fxtwitter + 上传图片，**不含 100k/天 的请求额度**，但很快会撞上限 |
| 内存 | 128 MB / isolate | 多图相册要把所有原图读进内存组装 multipart，**大图/大视频有爆内存风险**（Telegram 本身限制 50MB，需要自行加体积闸门） |
| 同时出站连接 | 6 / 请求 | 并发下载原图需要限流 |
| cron 单次时长 | 15 分钟 | 足够 |
| KV 免费额度 | 100,000 读 / 天，**1,000 写 / 天** | 状态必须**合并成一个键、每轮只写一次**（288 写/天）；按「每频道每轮写一次」会立刻超限（频道数 × 288） |
| D1 免费额度 | 500 万行读 / 10 万行写 / 天，5GB | 频道多、需要按条记账时用 D1 替代 KV |

**尚未实施的改造清单**（如果要做 Workers 版，缺的是这些）：

1. **运行形态**：cron 调度器 + 本地 `state.json` → Workers `scheduled()` + D1/KV；状态写入必须合并批处理（见上表 KV 写限制）。
2. **并发模型**：单进程顺序扫描 → 一个 dispatcher 把频道分片 fan-out 到子调用，每个子调用独立 10ms CPU 预算。
3. **大文件路径**：当前实现把图片整块读进内存再 multipart 上传；Workers 下需要流式或多段上传方案，否则相册/视频容易撞 128MB 内存。
4. **多租户**：当前是「自己部署给自己用」，频道列表写死在配置文件里，没有 `/start` 引导、没有按用户/频道的配额与限流、没有 webhook。
5. **私密频道**：检测依赖 `t.me/s` 公开预览页，私密频道不适用（需用户账号 MTProto 会话才有权威的 `web_preview` 数据）。
6. **群组 / 超级群**：实测不可行 —— `t.me/s/<公开超群>` 直接 302 且没有消息列表页；「编辑他人消息」是频道专属权限，超级群里机器人即使有该权限，`editMessageMedia` 仍返回 `Bad Request: message media can't be edited`。要覆盖群组必须换思路（用户账号检测 + 机器人另发消息）。
7. **图片处理**：不做压缩/转码，原图直接上传，由 Telegram 压到最长边 2560；需要缩略图/格式统一的话得自己加。

> 也就是说：**免费版能跑，但前提是先把上述 1–3 项改造做完**；不想改造的话，一台**能直连 X 与 Telegram 服务器**的最低配 VPS + cron 是成本最低、最稳的形态。

## 🔒 隐私与安全

- **Token 只放 `.env` / 环境变量**（`.env` 已在 `.gitignore` 里）；泄露就去 BotFather `/revoke` 换一个。
- 日志只记录频道用户名、message_id、体积与结果，**不打印 token**。
- 只申请必要的频道权限；不需要「发布/删除」时把 `multi_media` 设成 `mosaic`，权限可以只留「编辑消息」。
- 脚本不读写 Telegram 账号登录态：**不需要手机号、不需要 session 文件**，全部通过 Bot API 完成。
- **仓库自带隐私门禁**：`python scripts/check_privacy.py` 扫描跟踪文件里的本机绝对路径痕迹（CI 每次 push 都会跑；本地在仓库根目录执行一次 `git config core.hooksPath scripts/hooks` 即可在提交前拦截）。写示例路径请用无盘符占位（如 `目录A/子目录`）。

## ❓ 常见问题

**Q：能不能不装 Python、用现成服务？**
A：本项目只提供这个脚本。核心 trick（拿原图字节上传，而不是让 Telegram 去抓）任何语言都能实现，欢迎按你的栈重写。

**Q：为什么多图不能像单图那样原地替换，非要删了重发？**
A：Bot API 的 `editMessageMedia` 只接受单个媒体对象，无法给已有消息追加图片；相册只能由 `sendMediaGroup` 新建。所以「保真多图」必然伴随「删原帖 + 新消息通知」。不想发通知就用 `mosaic`（静默，但整张拼图也会被压到最长边 2560，多格拼图每格分辨率会下降）。

**Q：能不能修私密频道？**
A：不能。检测依赖 `t.me/s` 公开预览页；私密频道需要用户账号（MTProto）读取 `web_preview` 才有权威数据，那等于把账号登录态放进部署机，本项目刻意不这么做。

**Q：会不会误改我正常的帖文？**
A：判定基于「这条帖文在 Telegram 侧的预览里有没有图」，与帖文来源无关；已经带图/视频的帖文、纯文字帖都不会动。建议首次用 `--dry-run` 看一遍输出。

**Q：频控 / 限流怎么处理？**
A：每修一条之间固定间隔（默认 2 秒），机器人上传体积上限 45MB 留出余量；遇到失败会记日志并在下一轮重试（除非是「无媒体」这类确定性跳过）。

## 📁 项目结构

```
tg-thumb-fixer/
├── tg_thumb_fixer.py          # 主程序（单文件，零依赖）
├── config.example.json        # 配置模板
├── .env.example               # token 模板（.env 不入库）
├── deploy/
│   ├── cron.example           # cron 示例
│   ├── tg-thumb-fixer.service # systemd 单元（oneshot）
│   ├── tg-thumb-fixer.timer   # systemd 定时器
│   └── tg-thumb-fixer-watch.service  # 可选：常驻轮询形态
├── scripts/
│   ├── check_privacy.py       # 隐私门禁：检出本机绝对路径痕迹，命中即失败
│   └── hooks/pre-commit       # 本地钩子（git config core.hooksPath scripts/hooks 启用）
├── .github/workflows/ci.yml   # CI：隐私门禁 + 语法检查 + 冒烟
├── LICENSE
└── README.md
```

运行期产物（均已被 `.gitignore` 忽略）：`state.json`（幂等记账）、`fixer.log`、`fixer.lock`。

## 🤖 AIGC 声明

- 本项目代码、文档与测试用例均由 **AI 生成**（人机协作：由使用者提出需求、提供真实环境验证与反馈，AI 负责实现与整理）。
- 项目中的行为性数字（抓图体积阈值、页面体积、CPU 开销、Cloudflare 限额等）来自**真实环境实测或官方文档**，已在正文标注来源；即便如此，**请你在自己的环境里复测后再用于生产**。
- 使用本项目产生的任何后果（包括但不限于误改频道帖文、触发平台限制、账号/频道权限问题）由使用者自行承担。

## 📄 许可证

[MIT](LICENSE)
