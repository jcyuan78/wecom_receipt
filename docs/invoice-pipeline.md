# 发票自动识别与入库

> 适用：`WECOM_PROCESSOR=auto`。收到图片后，本地视觉模型先分类；发票进入「矫正 → 提取 → 去重 → 入库」流程，作业图片归档到另一个目录。一张照片里有多张发票时，会逐张分割、逐张入库。

---

## 1. 处理流程

```
企业微信图片
   │
   ▼
分类（Ollama 视觉模型）
   ├─ receipt  → 发票流程
   │              ① 检测纸张区域（1 张或多张）
   │              ② 逐张透视矫正 / 裁边 / 拉正
   │              ③ 逐张提取：日期、金额、明细、商家、发票号
   │              ④ 逐张去重（发票号 + 切片 sha256）
   │              ⑤ 逐张写入 SQLite，状态默认「未检查 / 未报销」
   │              ⑥ 每张单独存入 data/receipt/<年-月>/
   │
   ├─ homework → 移入 data/homework/<年-月>/
   └─ other    → 移入 data/other/<年-月>/
```

- 一张照片可含多张发票：先按纸张区域分割，再逐张识别、逐张入库，单图最多处理 `WECOM_INVOICE_MAX_REGIONS` 张（默认 6）。
- 分类失败或 Ollama 不可用：任务按退避策略重试（默认 3 次），文件留在 inbox，不会丢。
- 发票识别缺日期/金额：任务失败并重试；缺发票号/商家：仍入库，但标记 `needs_review=1`。

---

## 2. 配置

```ini
WECOM_PROCESSOR=auto

WECOM_OLLAMA_BASE_URL=http://127.0.0.1:11434
WECOM_OLLAMA_MODEL=qwen2.5vl:7b
WECOM_OLLAMA_TIMEOUT=300
WECOM_OLLAMA_KEEP_ALIVE=10m
WECOM_OLLAMA_NUM_CTX=8192

WECOM_RECEIPT_DIR=data/receipt
WECOM_HOMEWORK_DIR=data/homework
WECOM_OTHER_DIR=data/other

# 留空 = data/invoices.sqlite3
# WECOM_INVOICE_DB=data/invoices.sqlite3

WECOM_DEFAULT_CURRENCY=JPY
WECOM_INVOICE_MIN_CONFIDENCE=0.5
WECOM_RECEIPT_MAX_SIDE=2200
# 一张图最多拆成几张发票
WECOM_INVOICE_MAX_REGIONS=6
WECOM_KEEP_ORIGINAL=true
WECOM_NOTIFY_RESULTS=true
WECOM_NOTIFY_FAILURE=true
```

### 模型选择（重要）

| 模型 | 能否读图 | 说明 |
|---|---|---|
| `qwen2.5vl:7b` | ✅ | 推荐，中文/日文票据识别较稳 |
| `qwen2.5vl:3b` | ✅ | 显存小，速度更快，准确率略低 |
| `qwen3-vl:8b`（若已发布/已安装） | ✅ | 新一代，效果更好 |
| `qwen2.5:7b` / `qwen3:8b` | ❌ | **纯文本模型，不能读图** |

```powershell
ollama pull qwen2.5vl:7b
.\.venv\Scripts\python.exe tools\check_ai.py
.\.venv\Scripts\python.exe tools\check_ai.py --image data\receipt\2026-01\xxx.jpg
```

`check_ai.py` 会列出已安装模型、检查配置的模型是否存在、提示是否为视觉模型，并可用一张真实图片跑一遍分类 + 字段提取。

---

## 3. 目录结构

```
data/
├─ receipt/
│  ├─ 2026-01/
│  │  ├─ 2026-01-15_TEST STORE_INV-2026-001.jpg      # 单张图只有一张发票
│  │  ├─ 2026-01-15_TEST STORE_INV-2026-001.json
│  │  ├─ 2026-01-15_TEST STORE_INV-2026-001_p1.jpg   # 同一张图里的第 1 张
│  │  └─ 2026-01-15_SECOND SHOP_INV-2026-002_p2.jpg  # 第 2 张
│  ├─ _originals/                                    # 原始照片（可关闭）
│  └─ _duplicates/                                   # 重复发票
├─ homework/2026-01/...
├─ other/2026-01/...
└─ invoices.sqlite3
```

- 目录名按**发票日期**分月，不是收到日期。
- `_originals` 保留原始照片，便于换模型后重新识别。
- 重复发票不删，移到 `_duplicates` 留痕。

---

## 4. 数据库

`invoices` 表主要字段：

| 字段 | 说明 |
|---|---|
| `invoice_no` | 发票号，唯一（不区分大小写），用于防重复 |
| `invoice_date` | 开票/购买日期，`YYYY-MM-DD` |
| `amount_minor` | 金额，整数最小单位（JPY 即日元，CNY 为分） |
| `currency` | 币种 |
| `vendor` | 商店/供应商 |
| `items_json` | 明细 JSON |
| `tax_minor` | 税额 |
| `image_path` / `source_path` | 单张发票切片 / 原图 |
| `segment_index` | 同一张原图中的第几张（从 0 开始） |
| `segment_sha256` | 切片图哈希，唯一，用于重复检测 |
| `source_sha256` | 原图哈希，仅溯源；同一张图的多张发票共享同一个值 |
| `checked` / `checked_at` | 是否已检查 |
| `reimbursed` / `reimbursed_at` | 是否已报销 |
| `needs_review` | 是否需要人工复核 |
| `ai_model` / `ai_confidence` | 模型与置信度 |

金额用整数最小单位存储，避免浮点误差。查询时按币种分别合计。

---

## 5. 查询与状态管理

```powershell
# 本月发票 + 合计
python -m app.invoices query --from 2026-01-01 --to 2026-01-31

# 只给合计
python -m app.invoices total --from 2026-01 --to 2026-12

# 只看未检查 / 未报销
python -m app.invoices query --from 2026-01 --to 2026-12 --unchecked
python -m app.invoices total --from 2026-01 --to 2026-12 --unreimbursed

# 按供应商筛选
python -m app.invoices query --from 2026-01 --to 2026-12 --vendor テスト

# 标记为已检查、已报销
python -m app.invoices mark 12 --checked
python -m app.invoices mark 12 --reimbursed
python -m app.invoices mark --invoice-no INV-2026-001 --checked --reimbursed

# 取消标记
python -m app.invoices unmark 12 --reimbursed

# 查看详情 / 导出 CSV
python -m app.invoices show 12
python -m app.invoices export --from 2026-01 --to 2026-12 --output 2026.csv

# 数据库总览
python -m app.invoices stats
```

输出示例：

```
日期范围: 2026-01-01 ~ 2026-01-31
ID     日期        金额  供应商          发票号               查/报  内容
-----  ----------  ------------  --------------------  ------------------  -----  ----------------------------------
    1  2026-01-15    3,280 JPY  テスト商店      INV-2026-001        ··     ノート、笔
--------------------------------------------------------------------------------------------------------------
合计: 3,280 JPY（1 张）
```

`查/报` 两列分别是「已检查」「已报销」，`✓` 表示已完成。

其他选项：

- `--needs-review`：只看需人工复核的发票
- `--json`：输出 JSON，方便接前端或其他脚本
- `--db <路径>`：临时指定数据库
- `WECOM_INVOICE_DB`：永久覆盖数据库位置

---

## 6. 去重规则

逐张检查：

1. **相同切片**：`segment_sha256` 已存在 → 重复（同一张原图重复投递时最有效）。
2. **相同发票号**：`invoice_no` 已存在（忽略大小写）→ 重复。

`source_sha256` 只做溯源，不再唯一：一张原图里的多张发票共享同一个值。

重复时：

- 该张不再入库，切片移到 `data/receipt/_duplicates/`；
- `data/jobs/<MsgId>.json` 里 `processing.duplicates` 会列出重复项；
- 企业微信消息本身仍标记为处理完成（不会无限重试）。

没有发票号的发票仍会入库，但 `needs_review=1`，需要人工确认——这类发票无法靠发票号防重，只能靠切片哈希。

---

## 7. 图像矫正做了什么

`app/imaging.py`：

1. EXIF 方向纠正（手机照片）。
2. 亮度（Otsu）+ 边缘（Canny）两路找纸张区域，按面积、包含关系、重叠度合并去重。
3. 每个区域透视变换，把斜拍的纸拉正。
4. 裁掉桌面/背景，只留纸张；再裁掉纸张内部的空白边缘。
5. Hough 直线估计倾斜角（±20°），旋转校正。
6. 按最长边缩放（默认 2200px）。

**多张发票怎么判定**：每个候选区域面积需大于整图的 5%，且不能被另一个候选完全包含，互相重叠超过 50% 只保留一个；最多取 `WECOM_INVOICE_MAX_REGIONS` 张。

如果模型在返回结果里给出 `rotation`（90/180/270），会自动旋转后重试一次；仍缺关键字段时，再尝试 180°、90°。

失败兜底：

- 找不到纸张轮廓 → 整图当作一张，走裁边 + 倾斜校正。
- 分割出多张但全部识别失败 → 回退整图识别，避免误切导致丢票。
- 部分成功、部分失败 → 成功的入库，失败项写入 `processing.failed_segments` 并记日志，原图保留在 `_originals`。

---

## 8. 识别与日期策略

### 8.1 在整图上识别，裁剪只用于存档

早期版本先切图、再对切图做一次识别，实测有两个问题：

- 切图可能把日期/合计裁掉，识别反而变差；
- 同一张图要调用两次视觉模型，慢且容易触发空输出。

现在改成**一次性分析**：把 EXIF 纠正后的原图直接交给模型，同一个 JSON 里同时返回分类和发票字段。
切图只用于保存归档，不再参与识别（多张发票时除外，见 8.3）。

如果一次性分析缺日期或金额，才会在原图上做一次兜底识别（不会用切图）。

### 8.2 日期综合判断

发票日期的写法很杂（`令和7年11月18日`、`11/18`、`2026.1.5`、缺年份、日月顺序不明）。
`app/invoices/dates.py` 会：

1. 解析所有可能日期：公历、日本年号、两位年份、纯月日；
2. 用**收到消息的日期**作为先验，选择最接近、且不在未来的那个；
3. 缺年份时补收到日期的年份；若算出来在未来，则回退一年；
4. 年份明显写错（例如收到 2026 年却识别成 2027）时，用收到年份重试；
5. 无法判断时使用收到日期兜底，并标记 `needs_review=1`。

`fields.date_adjusted` 会记录是否做过推断；为真时数据库的 `needs_review=1`，方便人工抽查。

### 8.3 多张发票

一张图检测到多张时，仍然逐张切图、逐张识别（每张都需要独立的字段）。
单张图不会走切图识别。

### 8.4 上下文长度

图片会占用大量上下文。默认 `WECOM_OLLAMA_NUM_CTX=8192`；如果日志出现
`Ollama 返回了空内容`，先调大到 `12288` 或 `16384`，或把 `WECOM_RECEIPT_MAX_SIDE` 调小。

---

## 9. 处理结果回复

处理完成后，服务会通过企业微信**应用消息**（`message/send`）回复发送人：

- 单张成功：日期、商家、金额、发票号；
- 多张成功：逐条列出 + 合计；
- 重复发票：提示已录入，不重复入库；
- 识别失败（重试耗尽）：请用户重新拍摄，并给出拍摄建议。

相关配置：

```ini
WECOM_NOTIFY_RESULTS=true
WECOM_NOTIFY_FAILURE=true
```

说明：

- 使用的是自建应用发消息权限（默认具备），不需要额外后台配置；
- 回复目标就是消息发送人（`FromUserName`），必须在应用可见范围内；
- 通知失败只记日志，不影响发票入库。

---

## 10. 常见问题

| 现象 | 处理 |
|---|---|
| `无法连接 Ollama` | 确认 `ollama serve` 在跑，`WECOM_OLLAMA_BASE_URL` 正确 |
| `找不到模型` | `ollama pull qwen2.5vl:7b`，注意大小写与 tag |
| 模型能聊天但读不了图 | 用了纯文本模型（qwen3:8b），换 `qwen2.5vl` |
| 金额识别错 | 看 `needs_review`，换更大模型或提高图片清晰度 |
| 日期识别成乱码 | 模型可能在 90° 旋转图上失败，检查 `rotation_applied` |
| 同一张发票重复入库 | 发票号缺失时无法按号去重，需要人工补号或删记录 |
| 一张图里两张发票只识别出一张 | 两张纸有重叠，或桌面也是白色时可能合并；分开拍 / 换深色背景 |
| 一张发票被误切成多张 | 发票内部有独立大框时可能误判；全部失败会自动回退整图，也可设 `WECOM_INVOICE_MAX_REGIONS=1` |
| `Ollama 返回了空内容` | 调大 `WECOM_OLLAMA_NUM_CTX`（如 16384），或缩小 `WECOM_RECEIPT_MAX_SIDE` |
| 日期识别偏差 | 看 `needs_review` / `date_adjusted`；模型较小或票据手写时可换更大模型 |
| 任务一直失败 | `data/logs/receiver.log` 看 Ollama 报错；默认重试 3 次后标 failed |
| 想重跑旧发票 | 保留原图在 `_originals`，删对应 DB 行后可重新投递 |

---

## 11. 后续扩展

- 作业图片的分析（题目/答案结构化）。
- 发票明细按类别统计（餐饮/交通/办公）。
- 月度报表与 CSV/Excel 导出。
- 企业微信回执：处理完成后把识别结果发回给发送人。
- Web 查询界面（当前 CLI 已可直接接 API）。
