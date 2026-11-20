# 企业微信文件自动接收器

把别人发到你企业微信「文件助手」应用里的**图片 / 文件 / 语音 / 视频**，自动下载到本机，并立即触发下一阶段处理程序。

已完成：**接收 → 验签解密 → 入队 → 下载落盘 → 本地 AI 分类 → 发票识别入库 / 作业归档**。
发票支持自动矫正、日期/金额/商家/发票号提取、SQLite 去重入库、检查/报销状态，以及按日期范围查询合计。

---

## 1. 它能收到什么，收不到什么

企业微信没有"读取我个人聊天记录"的公开接口，所以先选对通道很重要：

| 方案 | 能收到谁发的文件 | 费用 / 门槛 | 本仓库 |
|---|---|---|---|
| **自建应用**（推荐先做） | 成员、同事发到**这个应用会话**里的图片/文件 | 免费，管理员建应用即可 | ✅ 已实现 |
| **微信客服** | **外部微信用户**发给客服账号的图片/文件 | 企业认证后可开通 | 未实现，核心下载逻辑可复用 |
| **会话内容存档** | 成员与同事/外部客户的**真实个人会话** | 企业认证 + 按人购买许可 + 官方 SDK | 未实现，见文档 |
| 个人微信 hook | 个人微信聊天 | 无官方接口，违反协议、有封号风险 | ❌ 不做 |

- 你想"别人直接发我企业微信，我就自动收"——严格意义上只有**会话内容存档**能覆盖个人会话，成本和合规门槛都高。
- 务实的起步方式：建一个自建应用「文件助手」，让同事/外部协作方把文件发给它，或让客服/助理转发给它。链路完全一样，且免费、稳定、不封号。
- **边界**：自建应用只面向本企业成员，外部微信用户加不了、也发不进来；外部群同样不支持添加自建应用或智能机器人。要收外部客户的文件请用**微信客服**，要覆盖真实个人会话请用**会话内容存档**。

后台配置的完整步骤（含截图级菜单路径、内网穿透、排错）见：
**[docs/wecom-backend-setup.md](docs/wecom-backend-setup.md)**

---

## 2. 已实现的能力

- **官方协议**：回调 URL 验签、AES-256-CBC 解密、被动回复加密，不依赖任何 hook。
- **5 秒内响应**：回调只做验签 + 解密 + 入 SQLite，下载放到后台线程，避免企业微信超时重试。
- **幂等**：按 `MsgId` 去重；企业微信重试不会重复下载、重复触发。
- **完整落盘**：保留原始文件名（含中文、RFC 5987 编码）、`sha256`、`meta.json`、按日期分目录。
- **安全**：路径穿越清洗、文件大小上限、DTD/XXE 防护、签名常量时间比较、文件名 basename 化。
- **可靠**：SQLite 队列 + 崩溃恢复（`working` 状态自动回收）+ 指数退避重试（默认 3 次）。
- **触发方式**：`noop`（写 job 文件）/ `command`（跑本地脚本）/ `http`（POST webhook）/ `auto`（本地 AI 分类）。
- **发票流水线**：单图多票自动分割、透视矫正、视觉模型提取、SQLite 入库、重复拦截、检查/报销状态、日期范围查询与合计。
- **图片自动分类**：按内容路由到 `data/receipt` / `data/homework` / `data/other`。
- **结果回复**：识别成功、重复、失败都会用应用消息回复发送人；失败时提示重新拍摄。
- **调试代理**：生产服务器只转发，开发机在内网跑完整流程，见 [docs/debug-proxy.md](docs/debug-proxy.md)。
- **可观测**：滚动日志、`/healthz` 队列统计、可选 `/api/messages` 调试接口。
- **可离线验收**：`tools/demo_local.py` 用模拟企业微信服务跑通完整加密回调；102 个自动化测试。

---

## 3. 数据流

```
企业微信用户 ──发图片/文件──▶ 企业微信服务器
                                  │  加密 POST（要求 5s 内响应）
                                  ▼
                    FastAPI  POST /wecom/callback
              验签 → 解密 → 解析 XML → 写入 SQLite 队列 → 立即 200 / 被动回复
                                  │
                                  ▼
                        后台 Worker 线程（每 1s 轮询）
                    media/get 下载 → 安全落盘 → 生成 job.json
                                  │
              ┌───────────────────┼───────────────────┐
              ▼                   ▼                   ▼
         noop（默认）         command              http
      监听 data/jobs 目录    执行本地分析脚本      POST 到你的服务
```

---

## 4. 快速开始

### 4.1 环境

- Python 3.10+（开发验证使用 3.12）
- Windows 10/11 或 Linux 均可
- 需要一台**常开**的机器，以及一个**公网可达的 80/443 地址**（本机测试可用 cloudflared 临时隧道）

### 4.2 安装

```powershell
cd C:\path\to\wecom_receiver
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

### 4.3 先跑本地演示（不需要企业微信账号）

```powershell
.\.venv\Scripts\python.exe tools\demo_local.py
```

它会启动一个模拟企业微信 API、一个真实的本服务，然后发送两条**加密回调**（一张图片 + 一个中文名文件）。成功时输出：

```
[4/4] 下载与触发结果
      队列状态: {"done": 2, "total": 2}
      ✓ image data\demo\inbox\2026-11-15\9001\screenshot.png  (70 bytes)
      ✓ file  data\demo\inbox\2026-11-15\9002\2026Q1-报表.txt  (25 bytes)
```

产物在 `data/demo/` 下，可以直接打开看。

### 4.4 跑测试

```powershell
.\.venv\Scripts\python.exe -m pytest
```

### 4.5 启用发票识别（需要 Ollama）

```powershell
ollama pull qwen2.5vl:7b
.\.venv\Scripts\python.exe tools\check_ai.py
```

在 `.env` 中设置 `WECOM_PROCESSOR=auto`。模型选择、目录、查询命令见
**[docs/invoice-pipeline.md](docs/invoice-pipeline.md)**。

> 注意：`qwen3:8b`、`qwen2.5:7b` 是纯文本模型，不能读图；请用 `qwen2.5vl:7b`、`qwen3-vl` 或 llava。
> 已经装过其他视觉模型（例如 `qwen2.5vl:3b`）时，把 `.env` 的 `WECOM_OLLAMA_MODEL` 改成该名称即可。

### 4.6 接入真实企业微信

1. 按 **[docs/wecom-backend-setup.md](docs/wecom-backend-setup.md)** 建自建应用、开通「接收消息」。
2. 生成密钥并填写 `.env`：

   ```powershell
   .\.venv\Scripts\python.exe tools\gen_keys.py
   copy .env.example .env
   notepad .env
   ```

3. 把服务暴露到公网（回调 URL 必须 80/443 可达）：

   ```powershell
   # 临时演示：每次重启域名会变
   winget install --id Cloudflare.cloudflared
   cloudflared tunnel --url http://127.0.0.1:8080
   ```

   拿到 `https://xxxx.trycloudflare.com` 后，回调 URL 填 `https://xxxx.trycloudflare.com/wecom/callback`。

4. 启动：

   ```powershell
   .\run.ps1
   ```

   监听地址与端口来自 `.env` 的 `WECOM_HOST` / `WECOM_PORT`（默认 `0.0.0.0:8080`）。

5. 在手机企业微信「工作台 → 文件助手」里发一张图片，观察：

   ```powershell
   curl http://127.0.0.1:8080/healthz
   dir data\inbox
   type data\jobs\<MsgId>.json
   ```

---

## 5. 配置项

全部通过环境变量或 `.env` 配置，前缀 `WECOM_`。

| 变量 | 必填 | 说明 |
|---|---|---|
| `WECOM_CORP_ID` | ✅ | 企业 ID，后台「我的企业 → 企业信息」 |
| `WECOM_AGENT_ID` | ✅ | 自建应用 AgentId |
| `WECOM_APP_SECRET` | ✅ | 自建应用 Secret |
| `WECOM_CALLBACK_TOKEN` | ✅ | 后台「接收消息」里的 Token |
| `WECOM_CALLBACK_AES_KEY` | ✅ | 43 位 EncodingAESKey |
| `WECOM_DATA_DIR` | | 数据目录，默认 `data` |
| `WECOM_HOST` / `WECOM_PORT` | | 监听地址/端口，默认 `0.0.0.0:8080` |
| `WECOM_MAX_FILE_MB` | | 单文件上限，默认 100 |
| `WECOM_AUTO_REPLY` | | 收到媒体后自动回复的文字，留空关闭 |
| `WECOM_PROCESSOR` | | `noop` / `command` / `http` / `auto` |
| `WECOM_PROCESSOR_COMMAND` | | `command` 模式执行的命令 |
| `WECOM_PROCESSOR_WEBHOOK_URL` | | `http` 模式的目标地址 |
| `WECOM_OLLAMA_MODEL` | auto | 视觉模型，默认 `qwen2.5vl:7b` |
| `WECOM_OLLAMA_BASE_URL` | | 默认 `http://127.0.0.1:11434` |
| `WECOM_OLLAMA_NUM_CTX` | | 视觉模型上下文，默认 8192 |
| `WECOM_NOTIFY_RESULTS` / `WECOM_NOTIFY_FAILURE` | | 是否把结果/失败原因回复给发送人，默认开启 |
| `WECOM_RECEIPT_DIR` / `WECOM_HOMEWORK_DIR` / `WECOM_OTHER_DIR` | | 分类目录 |
| `WECOM_INVOICE_DB` | | 发票库路径，默认 `data/invoices.sqlite3` |
| `WECOM_DEFAULT_CURRENCY` | | 默认 `JPY` |
| `WECOM_INVOICE_MAX_REGIONS` | | 单图最多拆几张发票，默认 6 |
| `WECOM_MODE` | | `normal` / `proxy`（调试转发） |
| `WECOM_DEBUG_PROXY_TARGET` / `WECOM_DEBUG_PROXY_TOKEN` | | proxy 模式：开发机地址与共享令牌 |
| `WECOM_MAX_ATTEMPTS` | | 失败重试次数，默认 3 |
| `WECOM_WORKER_ENABLED` | | 是否启动后台下载线程，默认 `true` |
| `WECOM_ADMIN_TOKEN` | | 开启 `/api/messages` 调试接口 |
| `WECOM_LOG_LEVEL` | | 默认 `INFO` |

完整示例见 [.env.example](.env.example)。

---

## 6. 下游处理契约（下一阶段从这里接）

每个下载成功的文件都会生成 `data/jobs/<MsgId>.json`：

```json
{
  "schema": "wecom.receiver.job/v1",
  "msg_id": "9001",
  "msg_type": "image",
  "from_user": "zhangsan",
  "to_user": "wwdemocorp000001",
  "agent_id": "1000002",
  "created_at": "2023-11-15T06:13:20+00:00",
  "received_at": "2026-11-15T12:20:33+00:00",
  "file": {
    "path": "C:\\...\\data\\inbox\\2026-11-15\\9001\\screenshot.png",
    "name": "screenshot.png",
    "size": 70,
    "sha256": "…",
    "content_type": "image/png"
  },
  "raw": { "MsgId": "9001", "MsgType": "image", "FromUserName": "zhangsan", "MediaId": "…" }
}
```

同一目录还有 `data/inbox/<日期>/<MsgId>/meta.json`，包含发送人、原始 XML 字段等。

### 四种触发方式

**noop**（最省事）：只写 job 文件，下一阶段自己监听目录，例如用 `watchdog`/`watchfiles`。

**command**：直接跑你的脚本，路径通过环境变量传入：

| 环境变量 | 含义 |
|---|---|
| `WECOM_FILE_PATH` | 文件绝对路径 |
| `WECOM_FILE_NAME` | 文件名 |
| `WECOM_FILE_SIZE` / `WECOM_FILE_SHA256` | 大小 / 摘要 |
| `WECOM_MSG_ID` / `WECOM_MSG_TYPE` / `WECOM_FROM_USER` | 消息元数据 |
| `WECOM_JOB_PATH` | job JSON 路径 |

```ini
WECOM_PROCESSOR=command
WECOM_PROCESSOR_COMMAND=python "C:\work\analyze_image.py"
```

`analyze_image.py` 里读 `os.environ["WECOM_FILE_PATH"]` 即可。命令退出码非 0 会按退避策略重试。

**http**：把同一个 job JSON POST 到你的服务，2xx 视为成功。

**auto**（发票/作业）：本地视觉模型先分类，发票矫正后提取字段并入库，作业图片归档。
一张图里有多张发票时会逐张分割、逐张入库，文件名带 `_p1`、`_p2` 后缀。
处理完成后 `job.file.path` 指向第一张结果，全部路径见 `processing.invoices`。配置与查询见
**[docs/invoice-pipeline.md](docs/invoice-pipeline.md)**。

> 建议：分析脚本按 `msg_id` 或 `file.sha256` 做幂等；同一文件因重试可能被处理多次。

---

## 7. 数据目录

```
data/
├─ inbox/                       # 刚下载、尚未处理
├─ jobs/                        # job JSON（auto 模式会更新为最终路径）
├─ receipt/                     # 发票：<年-月>/<日期>_<商家>_<发票号>.jpg + .json
│  ├─ _originals/               # 原始照片
│  └─ _duplicates/              # 重复发票
├─ homework/<年-月>/            # 作业图片
├─ other/<年-月>/               # 其他图片/文件
├─ invoices.sqlite3             # 发票数据库
├─ receiver.sqlite3             # 消息队列
└─ logs/receiver.log
```

---

## 8. HTTP 接口

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/wecom/callback` | 企业微信保存回调配置时的 URL 校验 |
| `POST` | `/wecom/callback` | 接收加密消息事件 |
| `GET` | `/healthz` | 健康检查 + 队列统计 |
| `GET` | `/api/messages?token=…` | 最近消息（需配置 `WECOM_ADMIN_TOKEN`） |

---

## 9. 可靠性设计

- **5 秒约束**：企业微信要求 5 秒内响应，超时会重试 3 次。本服务回调里不做下载，只入库，通常 < 20ms。
- **幂等**：`messages.msg_id` 主键 + `INSERT OR IGNORE`，重复回调直接 200。
- **崩溃恢复**：启动时把超过 10 分钟仍处于 `working` 的行恢复为待处理。
- **重试**：下载失败重新下载；处理失败只重跑处理器（文件还在）。退避 10s → 20s → 40s，默认 3 次后标记 `failed`。
- **media_id 有效期 3 天**：所以收到后立即下载，不依赖事后补拉。

---

## 10. 常见问题

| 现象 | 排查 |
|---|---|
| 后台保存回调提示"校验失败" | 服务没启动 / 隧道断了 / URL 路径不是 `/wecom/callback` / 用了非 80、443 端口 / Token、AESKey 与 `.env` 不一致 |
| 日志出现 403 签名校验失败 | 后台的 Token/AESKey 与 `.env` 不一致，改完要重启 |
| 接口报 60020 | 服务器出口 IP 没加入「企业可信IP」 |
| 收不到消息 | 应用可见范围不含发送人；「接收消息」未启用；回调配到了别的环境 |
| 文件下载失败 | `media_id` 过期（>3 天）、应用无权限、网络不通 |
| 中文文件名乱码 | 已按 RFC 5987 处理；如仍有问题请提供 `Content-Disposition` 原文 |
| Ollama 连接失败 / 找不到模型 | 确认 `ollama serve` 在运行；`ollama pull qwen2.5vl:7b`；`tools\check_ai.py` 可直接诊断 |

更多排错见后台配置文档最后一节。

---

## 11. 安全与合规

- 回调只信任通过签名校验的请求；密钥只存在 `.env`（已被 `.gitignore` 排除）。
- 落盘目录不要挂到 Web 静态目录；生产环境建议加反向代理、限流和磁盘配额。
- 如果后续改用**会话内容存档**：必须告知员工并取得同意，外部客户会话要符合《个人信息保护法》等要求，只采集业务必要范围，并设置保留期限。

---

## 12. 项目结构

```
app/
├─ main.py         应用工厂 / 生命周期
├─ callback.py     验签、解密、入队、被动回复
├─ crypto.py       AES-256-CBC + 签名
├─ xmlmsg.py       XML 解析 / 被动回复构造
├─ client.py       access_token 缓存 + media/get 下载
├─ store.py        SQLite 队列与状态机
├─ storage.py      安全落盘
├─ jobs.py         job JSON 契约
├─ processor.py    noop / command / http / auto
├─ worker.py       后台下载与触发
├─ classifier.py   图片分类（发票/作业/其他）
├─ pipeline.py     auto 路由
├─ imaging.py      发票透视矫正 / 裁边 / 拉正
├─ ollama.py       Ollama 视觉模型客户端
├─ invoices/       字段提取、SQLite、查询 CLI
└─ config.py       环境变量配置

tools/
├─ demo_local.py   无需企业微信的端到端演示
├─ mock_wecom.py   模拟 gettoken / media/get
├─ check_ai.py     检查 Ollama 与视觉模型
└─ gen_keys.py     生成 Token / EncodingAESKey

tests/             102 个单元 + 端到端测试
docs/              后台配置指南 + 发票流水线文档
```

---

## 13. 后续规划

1. ✅ **发票识别**：矫正、字段提取、去重入库、状态管理、日期范围查询。
2. **作业图片分析**：题目/答案结构化，接入 `data/homework`。
3. **归档与检索**：按发送人/日期/标签检索，月度报表。
4. **通知**：处理完成后用企业微信 `message/send` 把识别结果推回给发送人。
5. **多来源**：新增「微信客服」「会话内容存档」适配器，复用同一张 `messages` 表和下载器。
6. **识别率提升**：少样本、版式模板与 LoRA 微调的取舍，见 [docs/recognition-tuning.md](docs/recognition-tuning.md)。
