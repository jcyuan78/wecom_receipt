# 代码详细说明

> 适用版本：当前工作区代码（调试代理模式 + 本地文本消息处理改动）。与已提交的 `main` 可能存在少量差异，以实际代码为准。
> 目的：说明主要数据结构、程序主流程、各功能流程，以及流程中实际调用的函数。
> 阅读顺序建议：先看第 1、2 章的架构与配置，再看第 3 章的数据结构，最后按第 4、5 章的流程对照源码。

---

## 目录

1. [总体架构](#1-总体架构)
2. [配置结构 Settings](#2-配置结构-settings)
3. [主要数据结构](#3-主要数据结构)
4. [程序主流程](#4-程序主流程)
5. [各功能流程与关键函数](#5-各功能流程与关键函数)
6. [状态机与错误处理](#6-状态机与错误处理)
7. [测试与扩展点](#7-测试与扩展点)

---

## 1. 总体架构

### 1.1 运行形态

程序有两种运行形态，由 `WECOM_MODE` 控制：

| 形态 | `WECOM_MODE` | 职责 |
|---|---|---|
| 正常模式 | `normal` | 接收企业微信回调、下载、识别、入库、回复 |
| 调试代理 | `proxy` | 只转发 HTTP：企业微信 ↔ 开发机 |

正常模式：

```
企业微信
   │ 加密回调 / media/get / message/send
   ▼
FastAPI（app/main.py）
   ├─ callback.py     验签、解密、入队、被动回复
   ├─ worker.py       后台下载、落盘、触发处理器
   ├─ pipeline.py     分类、路由、发票/作业/其他
   └─ invoices/*      矫正、提取、去重、SQLite、查询
```

调试代理模式：

```
企业微信 ──▶ 生产服务器（proxy）──▶ 开发机（normal，完整程序）
                    ▲                    │
                    └── /debug/wecom-api ┘
```

### 1.2 模块职责

| 文件 | 职责 |
|---|---|
| `app/main.py` | 应用工厂、生命周期、normal/proxy 分支 |
| `app/config.py` | 环境变量配置 `Settings`、启动校验 |
| `app/callback.py` | 路由：回调、健康检查、调试 API 转发 |
| `app/proxy.py` | 调试代理的 HTTP 转发实现 |
| `app/crypto.py` | 企业微信 AES-256-CBC 加解密、签名 |
| `app/xmlmsg.py` | 回调 XML 解析、被动回复 XML 构造 |
| `app/models.py` | `Message`、`DownloadedMedia`、`SavedFile` |
| `app/client.py` | access_token 缓存、媒体下载、发送应用消息 |
| `app/storage.py` | 文件安全落盘、文件名清洗、meta.json |
| `app/store.py` | `messages` 表：队列与状态机 |
| `app/worker.py` | 后台线程：领取任务、下载、调用处理器 |
| `app/jobs.py` | job JSON 契约与原子写入 |
| `app/processor.py` | `Job`、`noop` / `command` / `http` 处理器 |
| `app/classifier.py` | 一次调用完成分类 + 发票字段提取 |
| `app/imaging.py` | 文档检测、多票分割、透视/裁边/拉正 |
| `app/ollama.py` | Ollama 客户端、JSON 解析、空响应处理 |
| `app/invoices/extract.py` | 提取提示词、`InvoiceFields`、旋转重试 |
| `app/invoices/dates.py` | 发票日期候选与综合推断 |
| `app/invoices/parsing.py` | 金额、币种、日期、发票号、明细解析 |
| `app/invoices/service.py` | 发票处理编排：切图、去重、保存、入库 |
| `app/invoices/db.py` | `invoices` 表、迁移、查询、状态 |
| `app/invoices/cli.py` | 查询 / 合计 / 标记 / 导出命令行 |
| `app/notify.py` | 结果与失败通知 |
| `app/logging_setup.py` | 控制台 + 滚动文件日志 |

---

## 2. 配置结构 Settings

`Settings`（`app/config.py`）基于 `pydantic-settings`，读取 `WECOM_` 前缀的环境变量或 `.env`。

### 2.1 字段分组

| 分组 | 字段 | 默认值 / 说明 |
|---|---|---|
| 企业微信凭据 | `corp_id`、`agent_id`、`app_secret` | 空，启动必填 |
| 回调 | `callback_token`、`callback_aes_key` | 空；AESKey 必须 43 位 |
| HTTP | `host`、`port`、`public_base_url` | `0.0.0.0:8080` |
| 存储 | `data_dir`、`max_file_mb` | `data`、100 |
| 企业微信 API | `wecom_api_base`、`http_timeout` | `https://qyapi.weixin.qq.com`、30s |
| 后台线程 | `worker_enabled`、`worker_poll_seconds`、`max_attempts` | `true`、1s、3 |
| 行为 | `auto_reply`、`notify_results`、`notify_failure` | 已收到文案、均 `true` |
| 处理器 | `processor`、`processor_command`、`processor_webhook_url`、`processor_timeout` | `noop`；另有 `command`/`http`/`auto` |
| Ollama | `ollama_base_url`、`ollama_model`、`ollama_timeout`、`ollama_keep_alive`、`ollama_num_ctx` | `127.0.0.1:11434`、`qwen2.5vl:7b`、300s、`10m`、8192 |
| 自动路由 | `receipt_dir`、`homework_dir`、`other_dir`、`invoice_db` | `data/receipt` 等；数据库默认 `data/invoices.sqlite3` |
| 发票 | `default_currency`、`invoice_min_confidence`、`receipt_max_side`、`invoice_max_regions`、`keep_original` | `JPY`、0.5、2200、6、`true` |
| 调试代理 | `mode`、`debug_proxy_target`、`debug_proxy_token`、`debug_proxy_timeout` | `normal`；proxy 时后两者必填 |
| 运维 | `admin_token`、`log_level` | 空、`INFO` |

### 2.2 派生路径

`Settings` 提供只读属性：

| 属性 | 结果 |
|---|---|
| `inbox_dir` | `<data_dir>/inbox` |
| `jobs_dir` | `<data_dir>/jobs` |
| `logs_dir` | `<data_dir>/logs` |
| `db_path` | `<data_dir>/receiver.sqlite3` |
| `invoice_db_path` | `invoice_db` 或 `<data_dir>/invoices.sqlite3` |

`ensure_dirs()` 创建数据目录、收件箱、job、日志以及三类分类目录。

### 2.3 启动校验

`validate_for_runtime()`：

- `mode=proxy`：只校验 `debug_proxy_target` 与 `debug_proxy_token`（`validate_proxy()`），然后返回；
- `mode=normal`：要求五个企业微信字段齐全、AESKey 43 位；
- 处理器为 `command` / `http` / `auto` 时，校验对应的命令、webhook 或模型名。

---

## 3. 主要数据结构

### 3.1 `Message`（`app/models.py`）

企业微信入站消息的统一表示，由 `xmlmsg.parse_message()` 生成。

| 字段 | 类型 | 说明 |
|---|---|---|
| `msg_id` | `str` | 企业微信 `MsgId`，主键/幂等键；事件类消息缺失时生成 `auto-<uuid>` |
| `msg_type` | `str` | `image` / `file` / `voice` / `video` / `text` / `event` 等 |
| `from_user` | `str` | 发送人 userid |
| `to_user` | `str` | 接收方（通常是 CorpID） |
| `agent_id` | `str` | 应用 AgentId |
| `create_time` | `int` | 企业微信消息时间（Unix 秒） |
| `media_id` | `str \| None` | 媒体文件 ID，下载用 |
| `file_name` | `str \| None` | 文件原始名 |
| `file_size` | `int \| None` | 文件大小 |
| `pic_url` | `str \| None` | 图片预览 URL |
| `media_format` | `str \| None` | 语音格式等 |
| `content` | `str \| None` | 文本内容 |
| `raw` | `dict[str,str]` | 原始 XML 字段 |

属性：

| 属性 | 含义 |
|---|---|
| `has_media` | 是否有 `media_id` |
| `is_media` | 类型属于 image/file/voice/video 且有 media_id |
| `is_text` | 文本消息且有内容 |
| `fallback_filename` | 没有文件名时的回退名 |
| `raw_public()` | 只保留可公开/可持久化的 XML 字段 |

> 说明：当前文本消息会入库为 `ignored`，`worker._handle_text()` 只写日志，尚未接入业务处理（见第 7 章）。

### 3.2 `DownloadedMedia` / `SavedFile`

| 结构 | 字段 | 说明 |
|---|---|---|
| `DownloadedMedia` | `content`、`filename`、`content_type`、`size` | `client.download_media()` 的返回值 |
| `SavedFile` | `path`、`name`、`size`、`sha256`、`content_type` | `FileStorage.save()` 的返回值 |

### 3.3 `RectifyResult`（`app/imaging.py`）

单张文档矫正结果：

| 字段 | 说明 |
|---|---|
| `image` | 矫正后的 PIL 图像 |
| `warped` | 是否做了透视变换 |
| `deskew_angle` | 估计并校正的倾斜角（度） |
| `source_size` / `output_size` | 原图/输出尺寸 |

`segment_documents()` 返回 `list[RectifyResult]`，一张图可对应多张发票。

### 3.4 `InvoiceFields`（`app/invoices/extract.py`）

发票字段的规范化结果：

| 字段 | 类型 | 说明 |
|---|---|---|
| `invoice_no` | `str \| None` | 发票号，去重键之一 |
| `invoice_date` | `date \| None` | 推断后的日期 |
| `amount_minor` | `int \| None` | 金额（最小货币单位） |
| `currency` | `str` | 币种，如 JPY |
| `vendor` | `str \| None` | 商家/供应商 |
| `items` | `list[dict]` | 明细 |
| `tax_minor` | `int \| None` | 税额 |
| `confidence` | `float` | 字段置信度 |
| `rotation` | `int` | 模型报告的旋转角 |
| `date_adjusted` | `bool` | 日期是否被推断/修正 |
| `document_type` | `str` | receipt / invoice |
| `raw` | `dict` | 模型原始 JSON |

关键属性/方法：

- `is_complete`：发票号、日期、金额、商家四项齐全；
- `score()`：字段完整度评分，用于旋转重试时选最优；
- `to_public_dict()`：写入边车 JSON 的字段。

### 3.5 `Classification`（`app/classifier.py`）

| 字段 | 说明 |
|---|---|
| `category` | `receipt` / `homework` / `other` |
| `confidence` | 分类置信度 |
| `reason` | 模型给出的简短理由 |
| `fields` | 分类为 receipt 时的 `InvoiceFields` |

### 3.6 `Job`（`app/processor.py`）与 job JSON

`Job` 是处理器看到的上下文：

| 字段 | 说明 |
|---|---|
| `data` | job JSON 字典 |
| `path` | `data/jobs/<MsgId>.json` 路径 |
| `msg_id` / `file_path` | 便捷属性 |
| `env()` | 供 `command` 处理器使用的环境变量 |

job JSON（`wecom.receiver.job/v1`）：

```json
{
  "schema": "wecom.receiver.job/v1",
  "msg_id": "9001",
  "msg_type": "image",
  "from_user": "zhangsan",
  "to_user": "ww...",
  "agent_id": "1000002",
  "created_at": "2023-11-15T06:13:20+00:00",
  "received_at": "2026-11-15T12:20:33+00:00",
  "file": {
    "path": "C:\\...\\data\\inbox\\...\\screenshot.png",
    "name": "screenshot.png",
    "size": 70,
    "sha256": "…",
    "content_type": "image/png"
  },
  "raw": {"MsgId": "9001", "MsgType": "image"},
  "processing": {
    "category": "receipt",
    "segments": 1,
    "invoice_count": 1,
    "invoices": [{"invoice_id": 1, "image_path": "…"}],
    "duplicates": [],
    "failed_segments": []
  }
}
```

`processing` 由 `AutoProcessor._update_job()` 写入；`file.path` 会更新为第一张结果图。

### 3.7 应用上下文

| 结构 | 字段 | 使用场景 |
|---|---|---|
| `AppContext`（`callback.py`） | `settings`、`crypto`、`store`、`storage`、`client`、`worker` | normal 模式 |
| `ProxyContext`（`proxy.py`） | `settings`、`client: httpx.AsyncClient` | proxy 模式 |

路由通过 `_ctx(request)` 取 `request.app.state.ctx`，再用 `isinstance(ctx, ProxyContext)` 区分两种模式。

### 3.8 `messages` 表（`data/receiver.sqlite3`）

一条入站消息一行，同时充当任务队列。

| 列 | 说明 |
|---|---|
| `msg_id` | 主键，幂等 |
| `msg_type`、`from_user`、`to_user`、`agent_id`、`create_time` | 消息元数据 |
| `received_at`、`updated_at` | 入库/更新时间（UTC ISO） |
| `status` | 见状态机 |
| `attempts` | 失败次数 |
| `next_attempt_at` | 下次可重试时间 |
| `media_id` | 媒体 ID |
| `filename`、`file_path`、`file_size`、`content_type`、`sha256` | 落盘信息 |
| `error` | 最近错误 |
| `raw_json`、`raw_xml` | 原始字段与明文 XML |
| `content` | 文本消息内容 |

索引：`(status, next_attempt_at)` 用于领取任务，`received_at` 用于最近消息。

### 3.9 `invoices` 表（`data/invoices.sqlite3`）

| 列 | 说明 |
|---|---|
| `id` | 自增主键 |
| `invoice_no` | 发票号，唯一（非空时，忽略大小写） |
| `invoice_date` | `YYYY-MM-DD` |
| `amount_minor` | 金额最小单位（JPY 为日元，CNY 为分） |
| `currency` | 币种 |
| `vendor` | 商家 |
| `items_json` | 明细 JSON |
| `tax_minor` | 税额 |
| `image_path` / `source_path` | 单张发票图 / 原图 |
| `source_msg_id` | 来源消息 |
| `source_sha256` | 原图哈希（仅溯源，不唯一） |
| `segment_index` | 同一原图中的第几张（0 开始） |
| `segment_sha256` | 切片图哈希，唯一，去重键 |
| `ai_model` / `ai_confidence` | 模型与置信度 |
| `needs_review` | 是否需人工复核 |
| `status` | new / review / reviewed / reimbursed |
| `source` | invoice / manual |
| `checked` / `checked_at` | 兼容旧字段，由 status 同步 |
| `reimbursed` / `reimbursed_at` | 兼容旧字段，由 status 同步 |
| `notes` / `raw_json` / `created_at` | 备注、原始 JSON、创建时间 |

索引：

- `ux_invoices_no`：`invoice_no` 唯一（非空）；
- `ux_invoices_segment`：`segment_sha256` 唯一（非空）；
- `ix_invoices_date`、`ix_invoices_status`、`ix_invoices_source`。

`InvoiceDB.init()` 会对旧库自动迁移：补 `segment_index` / `segment_sha256`、把旧的唯一 `source_sha256` 索引改成普通索引、用 `source_sha256` 回填 `segment_sha256`。

### 3.10 文件布局

```
data/
├─ inbox/<日期>/<MsgId>/<文件名> + meta.json   # 刚下载
├─ jobs/<MsgId>.json                           # job 契约
├─ receipt/<年-月>/<日期>_<商家>_<发票号>[_pN].jpg + .json
│  ├─ _originals/                              # 原始照片
│  └─ _duplicates/                             # 重复发票
├─ homework/<年-月>/...
├─ other/<年-月>/...
├─ receiver.sqlite3                            # 消息队列
├─ invoices.sqlite3                            # 发票库
└─ logs/receiver.log
```

---

## 4. 程序主流程

### 4.1 启动流程

入口：`uvicorn app.main:app` → `create_app()` → `lifespan()`。

normal：

```
create_app()
└─ lifespan()
   ├─ configure_logging(settings)
   ├─ settings.ensure_dirs()
   ├─ settings.validate_for_runtime()
   ├─ WeComCrypto(token, aes_key, corp_id)
   ├─ MessageStore(db_path).init()
   ├─ MessageStore.reset_stuck(600)
   ├─ WeComClient(corp_id, secret, api_base, ...)
   ├─ FileStorage(inbox_dir)
   ├─ build_processor(settings, wecom_client=client)
   │  └─ AutoProcessor / CommandProcessor / HttpProcessor / NoopProcessor
   ├─ Worker(settings, store, storage, client, processor)
   ├─ app.state.ctx = AppContext(...)
   ├─ worker.start()（worker_enabled=true）
   └─ yield
```

proxy：

```
lifespan()
├─ configure_logging / ensure_dirs / validate_for_runtime
│  └─ mode=proxy → validate_proxy()
├─ httpx.AsyncClient(timeout=debug_proxy_timeout)
├─ app.state.ctx = ProxyContext(settings, client)
└─ yield
```

不创建数据库、下载器、worker，也不写业务数据。

### 4.2 回调接收流程（normal）

#### GET `/wecom/callback`（URL 校验）

调用链：

```
verify_callback_url()
├─ _ctx(request)
├─ WeComCrypto.verify_signature(msg_signature, timestamp, nonce, echostr)
├─ WeComCrypto.decrypt(echostr)
└─ PlainTextResponse(plain)
```

proxy 模式下直接 `forward_to(ctx, request)` 转给开发机。

#### POST `/wecom/callback`（消息事件）

```
receive_message()
├─ _ctx(request)
├─ xmlmsg.extract_encrypt(body)
├─ WeComCrypto.verify_signature(...)
├─ WeComCrypto.decrypt(encrypt)
├─ xmlmsg.parse_message(plain) → Message
├─ MessageStore.add_message(message, plain)
│  └─ INSERT OR IGNORE（msg_id 幂等）
├─ 重复 → 200 空响应
├─ 媒体消息且 auto_reply 非空：
│  ├─ xmlmsg.build_text_reply_xml(...)
│  └─ WeComCrypto.encrypted_envelope(reply_xml)
└─ 文本消息 → 记录日志，200
```

约束：企业微信要求 5 秒内响应，因此这里只做验签、解密、入库和被动回复，不做下载/识别。

### 4.3 后台 worker 流程

`Worker.run()` 每 `worker_poll_seconds` 调用一次 `process_once()`。

```
process_once()
├─ MessageStore.claim_due(limit=5)
│  └─ UPDATE status='working' ... RETURNING *
└─ 对每行 _handle(row)
```

`_handle()`：

```
if msg_type == text:
    _handle_text(row)                 # 当前仅日志
else:
    if file_path 为空:
        saved = _download(row)
        │  ├─ WeComClient.download_media(media_id)
        │  ├─ FileStorage.save(message, media, received_at)
        │  └─ 写 meta.json
        └─ MessageStore.mark_downloaded(...)

    job_data = build_job(row)
    job_path = write_job(settings.jobs_dir, job_data)
    processor(Job(job_data, job_path)) # noop/command/http/auto
    MessageStore.mark_done(msg_id)
```

异常分支：

- `MediaTooLarge` → `mark_permanent_failure()` → 失败通知；
- 其他异常 → `mark_retry()`，达到 `max_attempts` 时返回 `"failed"` → 失败通知。

### 4.4 自动处理总流程（`WECOM_PROCESSOR=auto`）

`AutoProcessor.__call__(job)`：

```
AutoProcessor.__call__
├─ load_image(source)
├─ _received_date(job)                  # 收到日期，用于日期推断
├─ Classifier.analyze(image, received_date, default_currency)
│  ├─ prepare_for_model(image, 1600)
│  └─ OllamaClient.chat_json(ANALYZE_PROMPT)
├─ category=receipt → InvoiceService.process(job, source, image, analysis)
├─ category=homework → _move(homework_dir)
├─ category=other → _move(other_dir)
├─ _update_job(job, outcome)
└─ ResultNotifier.invoice_result(job.data, outcome)
```

注意：分类和字段提取是**一次模型调用**完成的；`analysis.fields` 会直接传给发票服务，单张发票不会对切图再次识别。

### 4.5 文字消息处理流程

`AutoProcessor.__call__` 在 `job.job_type == "text"` 时走：

```
TextMessageProcessor.process(job)
├─ OllamaClient.generate_json(PROMPT_ANALYZE, content)   # 消息 → 命令
├─ _execute(command)
│  ├─ query_invoices  → InvoiceDB.query
│  ├─ sum_invoices    → InvoiceDB.query + 分组汇总
│  ├─ mark_invoices   → InvoiceDB.update_status_by_ids
│  └─ create_manual_expense → InvoiceDB.add_manual
├─ OllamaClient.generate_text(PROMPT_RESPONSE, result)   # 结果 → 回复
└─ ResultNotifier.reply(from_user, reply)
```

文字消息在 `messages` 表中状态为 `pending`，由 worker 调用处理器后 `done`。

### 4.6 发票处理流程

`InvoiceService.process()`：

```
process()
├─ source_sha256 = sha256(原文件)
├─ _keep_original(job, source)          # 复制到 _originals
├─ segment_documents(image, max_side, max_regions)
│  ├─ detect_document_quads()
│  │  ├─ _paper_quads() / _edge_quads()
│  │  └─ _select_quads()
│  └─ 对每个区域 _perspective_warp → _crop_to_content → deskew → resize
├─ single = 段数 == 1
├─ precomputed = analysis.fields（仅 single）
└─ 对每段 _process_segment(...)
```

`_process_segment()`：

```
encode_jpeg(segment.image) → segment_sha256
├─ InvoiceDB.find_duplicate(None, segment_sha256)
│  └─ 命中 → _duplicate_segment()（写入 _duplicates）
├─ 若 fields_override 有日期+金额 → 直接使用（单张整图分析结果）
│  否则 extract_invoice(ai_image, received_date)
│     └─ build_extraction_prompt → OllamaClient.chat_json
│        → fields_from_dict → resolve_invoice_date
├─ InvoiceDB.find_duplicate(invoice_no, None)
│  └─ 命中 → _duplicate_segment()
├─ 校验日期/金额存在
├─ needs_review = 字段不全 OR 置信度低 OR date_adjusted OR 日期异常 OR 金额<=0
├─ _save_image() → receipt/<年-月>/...
├─ InvoiceDB.add(...)
│  └─ DuplicateInvoiceError → 删除临时图 → _duplicate_segment()
├─ _write_sidecar() → 同名 .json
└─ 返回 summary
```

多张图时对每段执行同样流程；如果所有段都失败，回退为整图识别一次（`rectify_document(image)`）。

最后：

```
_remove_source(source, original_path)   # 删除 inbox 原文件
返回 {category, segments, invoice_count, invoices, duplicates, failed_segments, ...}
```

### 4.7 结果通知流程

`ResultNotifier`：

```
AutoProcessor.__call__
└─ ResultNotifier.invoice_result(job.data, outcome)
   ├─ 仅重复 → 提示已录入
   ├─ 单张成功 → 日期/商家/金额/发票号
   ├─ 多张成功 → 逐条 + 合计
   ├─ 部分失败 → 追加“请补拍”
   └─ WeComClient.send_text(touser, text, agentid)
      └─ POST /cgi-bin/message/send
```

永久失败时：

```
Worker._handle → mark_retry() 返回 failed
└─ Worker._notify_failure()
   └─ AutoProcessor.notify_failure(row, error)
      └─ ResultNotifier.invoice_failure()
         └─ “❌ 发票识别失败，请重新拍摄后发送”
```

### 4.8 调试代理流程

生产服务器（`mode=proxy`）：

```
/wecom/callback（GET/POST）
└─ forward_to(ProxyContext, request)
   ├─ 目标 = debug_proxy_target + request.path
   ├─ 原样转发 query / body / headers
   └─ 返回开发机响应

/debug/wecom-api/{path}
├─ 校验 X-Debug-Token
├─ 目标 = wecom_api_base + path
└─ forward_to(..., drop_headers=("x-debug-token",))
```

开发机（`mode=normal`）：

```
WECOM_WECOM_API_BASE=http://<server>:8080/debug/wecom-api
WECOM_DEBUG_PROXY_TOKEN=<shared token>
```

`WeComClient` 在构造时把 `X-Debug-Token` 放进默认请求头，因此：

- `gettoken` / `media/get` / `message/send` 全部经生产服务器转发；
- 企业微信回调由生产服务器转发到开发机的 `/wecom/callback`，开发机正常验签、解密、入队、处理。

---

## 5. 各功能流程与关键函数

### 5.1 图片分析（分类 + 发票字段）

入口：`AutoProcessor.__call__` → `Classifier.analyze`。

```
Classifier.analyze(image, received_date, default_currency)
├─ prepare_for_model(image, 1600)      # 缩略 + JPEG
├─ OllamaClient.chat_json(ANALYZE_PROMPT, images=[...])
│  ├─ options: temperature=0, num_ctx=ollama_num_ctx
│  └─ 解析 message.content；为空时回退 message.thinking
├─ 类别归一化（_ALIASES）
├─ fields_from_dict(..., confidence_key="field_confidence")
│  ├─ normalize_currency / parse_amount / to_minor
│  ├─ resolve_invoice_date
│  └─ clean_invoice_no / parse_items
└─ Classification(category, confidence, reason, fields)
```

### 5.2 图像矫正与多票分割

入口：`InvoiceService.process` → `segment_documents`；单张回退用 `rectify_document`。

```
detect_document_quads(image, max_regions)
├─ _resize_for_detection(gray, 1600)
├─ _paper_quads(small)     # Otsu 亮度找纸张
├─ _edge_quads(small)      # Canny 找矩形边框
└─ _select_quads(...)      # 面积>=5%、去包含、去 IoU>0.5、排序

segment_documents(image, max_side, max_regions)
├─ detect_document_quads()
└─ 每个 quad：
   ├─ _perspective_warp()   # 四点透视 + 0.995 内缩
   ├─ _crop_to_content()    # 去白边
   ├─ deskew()              # Hough 估计角度 + 旋转
   └─ 缩放到 max_side
```

无候选时整图作为一张；分割全失败时回退整图。

### 5.3 日期综合判断

入口：`fields_from_dict` → `resolve_invoice_date`。

```
resolve_invoice_date(raw, received)
├─ date_candidates(raw, received)
│  ├─ 日本年号（令和/平成/昭和...）
│  ├─ YYYY-MM-DD / YYYY年M月D日 / YYYYMMDD
│  ├─ M/D/YYYY（日月歧义时生成两个候选）
│  └─ 无年份的 M月D日（补收到年份、上一年）
├─ _score(candidate, received)  # 未来、过旧、距离、是否显式年份
├─ 全为未来 → 用收到年份/上一年重试
└─ 返回 (date, adjusted)
```

`adjusted=true` 会在入库时把 `needs_review` 置 1。

### 5.4 金额、币种、明细

`app/invoices/parsing.py`：

| 函数 | 作用 |
|---|---|
| `normalize_currency(value, default)` | ¥/円→JPY，元/RMB→CNY，$→USD |
| `parse_amount(value)` | 去逗号/符号，返回 float |
| `to_minor(amount, currency)` | 转最小单位（JPY 0 位，CNY/USD 2 位） |
| `format_money(minor, currency)` | 展示格式，如 `3,280 JPY` |
| `clean_invoice_no(value)` | 去前缀、空格、非法字符 |
| `parse_items(value)` | 明细归一化 |
| `parse_confidence(value, default)` | 置信度 0–1 |

### 5.5 去重

两道检查，缺一不可：

1. **切片哈希**：`sha256(矫正后 JPEG)` → `segment_sha256`，唯一索引；
2. **发票号**：`invoice_no` 唯一索引（忽略大小写）。

重复时不再入库，图片写入 `data/receipt/_duplicates/`，job 的 `processing.duplicates` 记录原因与 `duplicate_of`。

### 5.6 发票查询 CLI

入口：`python -m app.invoices` → `cli.main()`。

```
main()
├─ Settings()
├─ InvoiceDB(invoice_db_path).init()
└─ args.func(args, db, settings)

query  → _cmd_query  → db.query / db.totals → _print_rows
total  → _cmd_total  → db.totals / db.query
show   → _cmd_show   → db.get / db.get_by_invoice_no
mark   → _cmd_mark   → db.set_status(checked=..., reimbursed=...)
unmark → _cmd_mark   → db.set_status(False/False)
export → _cmd_export → db.query + CSV（UTF-8 BOM）
stats  → _cmd_stats  → db.stats
```

日期范围支持 `YYYY-MM-DD`、`YYYY-MM`、`YYYY`；过滤支持 `--unchecked`、`--unreimbursed`、`--needs-review`、`--vendor`。

### 5.7 训练数据导出

`tools/export_training_data.py`：

```
InvoiceDB.query(2000-01-01, today)
└─ 每条：复制 image_path 到 images/，写 dataset.jsonl
   ├─ images
   ├─ messages（user/assistant）
   └─ fields（invoice_no/date/vendor/currency/amount_minor/tax_minor/items）
```

用于 few-shot 或 LoRA 数据准备，见 `docs/recognition-tuning.md`。

### 5.8 健康检查与调试接口

| 接口 | normal | proxy |
|---|---|---|
| `GET /healthz` | corp_id、worker_alive、队列统计 | mode、target |
| `GET /api/messages` | 需 `WECOM_ADMIN_TOKEN` | 403 |
| `/debug/wecom-api/*` | 404 | token 校验后转发 |

---

## 6. 状态机与错误处理

### 6.1 `messages` 状态

```
add_message
├─ 媒体消息 → pending
└─ 文本/事件 → ignored

pending ──claim_due──▶ working
downloaded ─claim_due─▶ working
working ──mark_downloaded──▶ downloaded
working/downloaded ──mark_done──▶ done

失败：
working/downloaded ──mark_retry──▶
  ├─ attempts < max：pending（无文件）或 downloaded（有文件），next_attempt_at = now + 10/20/40…s
  └─ attempts >= max：failed

working ──reset_stuck(>600s)──▶ pending/downloaded
```

### 6.2 失败与通知

- 下载失败：重试下载；
- 处理失败：文件已存在时只重跑处理器；
- `MediaTooLarge`：直接永久失败；
- 永久失败：`Worker._notify_failure()` → 用户收到“请重新拍摄”；
- 通知失败只记日志，不影响任务状态。

### 6.3 安全要点

- 回调必须通过 `WeComCrypto.verify_signature`，AESKey 解密后校验 `receive_id`；
- 文件名 basename 化 + 路径边界检查，防止路径穿越；
- 调试 API 必须带 `X-Debug-Token`；
- `run.ps1` 使用 `--no-access-log`，避免代理转发时 `corpsecret` 出现在访问日志；
- `.env`、`data/` 已在 `.gitignore` 中。

---

## 7. 测试与扩展点

### 7.1 测试结构（113 项）

| 文件 | 覆盖 |
|---|---|
| `tests/test_units.py` | 加密、XML、文件名、Store |
| `tests/test_pipeline.py` | 回调 → 下载 → job |
| `tests/test_auto_pipeline.py` | 分类、单票/多票、去重、通知、失败 |
| `tests/test_invoice_units.py` | 解析、DB、CLI、迁移 |
| `tests/test_invoice_imaging.py` | 透视、裁边、倾斜 |
| `tests/test_dates.py` | 日期候选与推断 |
| `tests/test_ollama.py` | 空响应、thinking 回退、num_ctx |
| `tests/test_proxy.py` | 调试转发、token、健康检查 |

### 7.2 已知限制与扩展点

| 项 | 说明 |
|---|---|
| 文本消息 | 入库为 `ignored`，`_handle_text()` 目前只记日志；需要时可在此接入命令/文本分析 |
| 多票重叠 | 两张纸重叠或白色背景融合时可能只识别一张 |
| 代理媒体 | 经生产服务器缓冲转发，占内存/带宽；大文件可改为流式 |
| 代理可用性 | 开发机宕机时返回 502，不排队；企业微信重试 3 次后放弃 |
| 发票模型 | 小模型（3B）对手写/复杂版式有限；可换 7B 或做 few-shot/模板 |
| 扩展处理器 | 在 `processor.build_processor()` 增加分支，或实现可调用的 Processor |
| 扩展来源 | 微信客服/会话存档可复用 `MessageStore`、`FileStorage`、job 契约 |
