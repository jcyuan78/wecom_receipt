"""Export labeled invoices for few-shot examples or LoRA fine-tuning.

    .venv\\Scripts\\python.exe tools\\export_training_data.py
    .venv\\Scripts\\python.exe tools\\export_training_data.py --output data\\training --limit 200

Output:
    data/training/dataset.jsonl
    data/training/images/000001.jpg
    data/training/README.md

Review the images before publishing or uploading them anywhere: they are real
invoices and may contain personal or customer information.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

from app.config import Settings  # noqa: E402
from app.invoices.db import InvoiceDB  # noqa: E402

PROMPT = (
    "Read this receipt or invoice image and return one JSON object with: "
    "invoice_no, date (YYYY-MM-DD), vendor, currency, total, tax, items. "
    "Use null when a value is not visible."
)


def main() -> int:
    parser = argparse.ArgumentParser(description="导出发票训练/少样本数据")
    parser.add_argument("--db", type=Path, help="默认取 .env 的发票数据库")
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "training")
    parser.add_argument("--limit", type=int, default=0, help="最多导出多少条（0=全部）")
    args = parser.parse_args()

    settings = Settings()
    db = InvoiceDB(args.db or settings.invoice_db_path)
    db.init()

    try:
        rows = db.query(
            date(2000, 1, 1),
            date.today(),
            limit=args.limit or None,
        )
    finally:
        db.close()

    output = args.output
    images_dir = output / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    exported = 0
    skipped = 0
    dataset_path = output / "dataset.jsonl"

    with dataset_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            source = Path(str(row.get("image_path") or ""))
            if not source.exists():
                skipped += 1
                continue

            suffix = source.suffix.lower() or ".jpg"
            name = f"{int(row['id']):06d}{suffix}"
            shutil.copy2(source, images_dir / name)

            fields = {
                "invoice_no": row.get("invoice_no"),
                "date": row.get("invoice_date"),
                "vendor": row.get("vendor"),
                "currency": row.get("currency"),
                "amount_minor": row.get("amount_minor"),
                "tax_minor": row.get("tax_minor"),
                "items": row.get("items") or [],
            }
            entry = {
                "images": [f"images/{name}"],
                "messages": [
                    {"role": "user", "content": PROMPT},
                    {
                        "role": "assistant",
                        "content": json.dumps(fields, ensure_ascii=False),
                    },
                ],
                "fields": fields,
                "meta": {
                    "invoice_id": row["id"],
                    "source_msg_id": row.get("source_msg_id"),
                    "needs_review": bool(row.get("needs_review")),
                    "checked": bool(row.get("checked")),
                    "reimbursed": bool(row.get("reimbursed")),
                },
            }
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            exported += 1

    (output / "README.md").write_text(
        "# 发票训练数据\n\n"
        f"- `dataset.jsonl`：{exported} 条\n"
        f"- `images/`：对应图片\n\n"
        "每行包含 `images`、`messages`（user/assistant）和 `fields`。\n"
        "可直接改造成 ms-swift / LLaMA-Factory 的 VLM 数据格式，用于 Qwen2.5-VL 的 LoRA 微调；\n"
        "也可以把其中 5–10 张同版式发票作为 few-shot 示例。\n\n"
        "## 注意\n\n"
        "- 图片是真实发票，可能包含个人信息/客户信息，上传或共享前务必脱敏。\n"
        "- 建议保留 20% 作为评测集，不要全部用于训练。\n"
        "- `needs_review=true` 的样本先人工核对字段，再用于训练。\n",
        encoding="utf-8",
    )

    print(f"已导出 {exported} 条到 {output}（跳过 {skipped} 条缺失图片）")
    print(f"数据文件: {dataset_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
