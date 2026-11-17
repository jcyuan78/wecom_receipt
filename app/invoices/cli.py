"""Command line tool for the invoice database.

Examples:
    python -m app.invoices query --from 2026-01-01 --to 2026-01-31
    python -m app.invoices total --from 2026-01
    python -m app.invoices mark 12 --checked
    python -m app.invoices mark --invoice-no INV-001 --reimbursed
    python -m app.invoices export --from 2026-01 --to 2026-12 --output 2026.csv
"""

from __future__ import annotations

import argparse
import calendar
import csv
import json
import re
import sys
import unicodedata
from datetime import date
from pathlib import Path
from typing import Any

from ..config import Settings
from .db import InvoiceDB
from .parsing import format_money

DEFAULT_START = date(2000, 1, 1)


# ----------------------------------------------------------------- formatting
def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in text)


def _pad(text: str, width: int, *, align: str = "left") -> str:
    padding = max(0, width - _width(text))
    if align == "right":
        return " " * padding + text
    return text + " " * padding


def _truncate(text: str, width: int) -> str:
    if _width(text) <= width:
        return text
    result = ""
    current = 0
    for char in text:
        step = 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        if current + step > width - 1:
            break
        result += char
        current += step
    return result + "…"


def _summary(row: dict[str, Any]) -> str:
    names = [str(item.get("name", "")) for item in row.get("items") or [] if item.get("name")]
    if not names:
        return ""
    return _truncate("、".join(names[:3]), 34)


def _flags(row: dict[str, Any]) -> str:
    return f"{'✓' if row.get('checked') else '·'}{'✓' if row.get('reimbursed') else '·'}"


def _print_rows(rows: list[dict[str, Any]], totals: list[dict[str, Any]]) -> None:
    header = ["ID", "日期", "金额", "供应商", "发票号", "查/报", "内容"]
    widths = [5, 10, 16, 22, 18, 6, 34]
    print("  ".join(_pad(title, width) for title, width in zip(header, widths)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print(
            "  ".join(
                [
                    _pad(str(row["id"]), widths[0], align="right"),
                    _pad(str(row["invoice_date"]), widths[1]),
                    _pad(format_money(int(row["amount_minor"]), str(row["currency"])), widths[2], align="right"),
                    _pad(_truncate(str(row.get("vendor") or "-"), widths[3]), widths[3]),
                    _pad(_truncate(str(row.get("invoice_no") or "-"), widths[4]), widths[4]),
                    _pad(_flags(row), widths[5]),
                    _pad(_summary(row), widths[6]),
                ]
            )
        )
    print("-" * 110)
    _print_totals(totals, len(rows))


def _print_totals(totals: list[dict[str, Any]], count: int) -> None:
    if not totals:
        print("合计: 0（0 张）")
        return
    parts = [format_money(int(item["total_minor"]), str(item["currency"])) for item in totals]
    print(f"合计: {' / '.join(parts)}（{count} 张）")


# ------------------------------------------------------------------ arguments
def _add_range(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--from", dest="start", help="起始日期，含（YYYY-MM-DD / YYYY-MM / YYYY）")
    parser.add_argument("--to", dest="end", help="结束日期，含（YYYY-MM-DD / YYYY-MM / YYYY）")


def _add_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--vendor", help="按供应商模糊筛选")
    parser.add_argument("--unchecked", action="store_true", help="只看未检查")
    parser.add_argument("--unreimbursed", action="store_true", help="只看未报销")
    parser.add_argument("--needs-review", action="store_true", help="只看需要人工复核")


def _parse_bound(text: str | None, *, upper: bool) -> date:
    if not text:
        return date.today() if upper else DEFAULT_START
    text = text.strip()
    if re.fullmatch(r"\d{4}", text):
        year = int(text)
        return date(year, 12, 31) if upper else date(year, 1, 1)
    match = re.fullmatch(r"(\d{4})[-/](\d{1,2})", text)
    if match:
        year, month = int(match.group(1)), int(match.group(2))
        last = calendar.monthrange(year, month)[1]
        return date(year, month, last if upper else 1)
    return date.fromisoformat(text.replace("/", "-"))


def _filters(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "only_unchecked": getattr(args, "unchecked", False),
        "only_unreimbursed": getattr(args, "unreimbursed", False),
        "needs_review": getattr(args, "needs_review", False),
        "vendor": getattr(args, "vendor", None),
    }


# ------------------------------------------------------------------- commands
def _cmd_query(args: argparse.Namespace, db: InvoiceDB, settings: Settings) -> int:
    start, end = _parse_bound(args.start, upper=False), _parse_bound(args.end, upper=True)
    filters = _filters(args)
    rows = db.query(start, end, **filters, limit=args.limit)
    totals = db.totals(start, end, **filters)

    if args.json:
        print(
            json.dumps(
                {
                    "range": {"from": start.isoformat(), "to": end.isoformat()},
                    "count": len(rows),
                    "items": rows,
                    "totals": totals,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print(f"日期范围: {start.isoformat()} ~ {end.isoformat()}")
    _print_rows(rows, totals)
    return 0


def _cmd_total(args: argparse.Namespace, db: InvoiceDB, settings: Settings) -> int:
    start, end = _parse_bound(args.start, upper=False), _parse_bound(args.end, upper=True)
    filters = _filters(args)
    totals = db.totals(start, end, **filters)
    rows = db.query(start, end, **filters)
    if args.json:
        print(json.dumps({"range": {"from": start.isoformat(), "to": end.isoformat()}, "totals": totals, "count": len(rows)}, ensure_ascii=False, indent=2))
        return 0
    print(f"日期范围: {start.isoformat()} ~ {end.isoformat()}")
    _print_totals(totals, len(rows))
    return 0


def _cmd_show(args: argparse.Namespace, db: InvoiceDB, settings: Settings) -> int:
    record = db.get(args.id) if args.id else db.get_by_invoice_no(args.invoice_no or "")
    if record is None:
        print("未找到该发票", file=sys.stderr)
        return 2
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


def _cmd_mark(args: argparse.Namespace, db: InvoiceDB, settings: Settings, value: bool) -> int:
    if not args.checked and not args.reimbursed:
        print("请至少指定 --checked 或 --reimbursed", file=sys.stderr)
        return 2
    if not args.id and not args.invoice_no:
        print("请提供发票 ID 或 --invoice-no", file=sys.stderr)
        return 2

    record = db.get(args.id) if args.id else db.get_by_invoice_no(args.invoice_no or "")
    if record is None:
        print("未找到该发票", file=sys.stderr)
        return 2

    updated = db.set_status(
        int(record["id"]),
        checked=value if args.checked else None,
        reimbursed=value if args.reimbursed else None,
    )
    assert updated is not None
    print(
        f"#{updated['id']} {updated['invoice_date']} "
        f"{format_money(int(updated['amount_minor']), str(updated['currency']))} "
        f"检查={'是' if updated['checked'] else '否'} "
        f"报销={'是' if updated['reimbursed'] else '否'}"
    )
    return 0


def _cmd_export(args: argparse.Namespace, db: InvoiceDB, settings: Settings) -> int:
    start, end = _parse_bound(args.start, upper=False), _parse_bound(args.end, upper=True)
    rows = db.query(start, end, **_filters(args))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["ID", "日期", "金额", "币种", "供应商", "发票号", "内容", "已检查", "已报销", "需复核", "图片"]
        )
        for row in rows:
            writer.writerow(
                [
                    row["id"],
                    row["invoice_date"],
                    format_money(int(row["amount_minor"]), str(row["currency"])),
                    row["currency"],
                    row.get("vendor") or "",
                    row.get("invoice_no") or "",
                    _summary(row),
                    "是" if row["checked"] else "否",
                    "是" if row["reimbursed"] else "否",
                    "是" if row["needs_review"] else "否",
                    row.get("image_path") or "",
                ]
            )
    print(f"已导出 {len(rows)} 条到 {output}")
    return 0


def _cmd_stats(args: argparse.Namespace, db: InvoiceDB, settings: Settings) -> int:
    print(json.dumps(db.stats(), ensure_ascii=False, indent=2))
    return 0


# --------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.invoices",
        description="发票数据库查询与状态管理",
    )
    parser.add_argument("--db", type=Path, help="覆盖数据库路径（默认取 .env 的 WECOM_INVOICE_DB）")
    subparsers = parser.add_subparsers(dest="command", required=True)

    query = subparsers.add_parser("query", help="按日期范围查询发票与合计")
    _add_range(query)
    _add_filters(query)
    query.add_argument("--limit", type=int, default=None)
    query.add_argument("--json", action="store_true", help="输出 JSON")
    query.set_defaults(func=_cmd_query)

    total = subparsers.add_parser("total", help="只输出合计金额")
    _add_range(total)
    _add_filters(total)
    total.add_argument("--json", action="store_true")
    total.set_defaults(func=_cmd_total)

    show = subparsers.add_parser("show", help="查看单张发票详情")
    show.add_argument("id", nargs="?", type=int)
    show.add_argument("--invoice-no")
    show.set_defaults(func=_cmd_show)

    mark = subparsers.add_parser("mark", help="标记为已检查/已报销")
    mark.add_argument("id", nargs="?", type=int)
    mark.add_argument("--invoice-no")
    mark.add_argument("--checked", action="store_true")
    mark.add_argument("--reimbursed", action="store_true")
    mark.set_defaults(func=lambda a, d, s: _cmd_mark(a, d, s, True))

    unmark = subparsers.add_parser("unmark", help="取消已检查/已报销标记")
    unmark.add_argument("id", nargs="?", type=int)
    unmark.add_argument("--invoice-no")
    unmark.add_argument("--checked", action="store_true")
    unmark.add_argument("--reimbursed", action="store_true")
    unmark.set_defaults(func=lambda a, d, s: _cmd_mark(a, d, s, False))

    export = subparsers.add_parser("export", help="导出 CSV")
    _add_range(export)
    _add_filters(export)
    export.add_argument("--output", default="invoices.csv")
    export.set_defaults(func=_cmd_export)

    stats = subparsers.add_parser("stats", help="查看数据库统计")
    stats.set_defaults(func=_cmd_stats)

    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass

    parser = build_parser()
    args = parser.parse_args(argv)

    settings = Settings()
    db_path = args.db or settings.invoice_db_path
    db = InvoiceDB(db_path)
    db.init()
    try:
        return int(args.func(args, db, settings) or 0)
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
