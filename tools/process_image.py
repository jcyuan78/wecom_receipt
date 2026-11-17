"""Process one local image through the auto pipeline (no WeCom needed).

Useful for testing, or for importing photos that were not received via WeCom.

    .venv\\Scripts\\python.exe tools\\process_image.py --file data\\test_invoice.jpg
    .venv\\Scripts\\python.exe tools\\process_image.py --file a.jpg --data-dir data\\import
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from datetime import date, datetime, timezone
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
from app.invoices.parsing import format_money  # noqa: E402
from app.jobs import write_job  # noqa: E402
from app.pipeline import AutoProcessor  # noqa: E402
from app.processor import Job  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="用本地图片跑一遍 auto 流水线")
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data" / "local")
    parser.add_argument("--model", help="覆盖 .env 中的 WECOM_OLLAMA_MODEL")
    args = parser.parse_args()

    source = args.file
    if not source.exists():
        print(f"文件不存在: {source}", file=sys.stderr)
        return 2

    base = args.data_dir
    settings = Settings().model_copy(
        update={
            "processor": "auto",
            "data_dir": base,
            "receipt_dir": base / "receipt",
            "homework_dir": base / "homework",
            "other_dir": base / "other",
            "invoice_db": base / "invoices.sqlite3",
            "worker_enabled": False,
        }
    )
    if args.model:
        settings = settings.model_copy(update={"ollama_model": args.model})
    settings.ensure_dirs()

    msg_id = f"local-{int(time.time())}"
    inbox = settings.inbox_dir / msg_id
    inbox.mkdir(parents=True, exist_ok=True)
    copy = inbox / source.name
    shutil.copy2(source, copy)

    payload = copy.read_bytes()
    job_data = {
        "schema": "wecom.receiver.job/v1",
        "msg_id": msg_id,
        "msg_type": "image",
        "from_user": "local-import",
        "to_user": "",
        "agent_id": "",
        "created_at": None,
        "received_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "file": {
            "path": str(copy.resolve()),
            "name": copy.name,
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "content_type": "image/jpeg",
        },
        "raw": {},
    }
    job_path = write_job(settings.jobs_dir, job_data)
    job = Job(job_data, job_path)

    processor = AutoProcessor(settings)
    try:
        processor(job)
    finally:
        processor.close()

    print(json.dumps(job.data.get("processing", {}), ensure_ascii=False, indent=2))

    db = InvoiceDB(settings.invoice_db_path)
    db.init()
    try:
        rows = db.query(date(2000, 1, 1), date.today())
        if rows:
            total = sum(int(row["amount_minor"]) for row in rows if row["currency"] == "JPY")
            print(f"\n数据库现有 {len(rows)} 张，JPY 合计 {format_money(total, 'JPY')}")
            for row in rows:
                print(
                    f"  #{row['id']} {row['invoice_date']} {row['vendor']} "
                    f"{row['invoice_no']} {format_money(int(row['amount_minor']), row['currency'])}"
                )
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
