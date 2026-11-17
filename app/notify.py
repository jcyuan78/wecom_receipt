"""Send invoice processing results back to the WeCom user."""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from .client import WeComClient
from .config import Settings
from .invoices.parsing import format_money

log = logging.getLogger(__name__)


class ResultNotifier:
    def __init__(self, client: WeComClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings

    def _send(self, to_user: str, text: str) -> None:
        if not to_user:
            return
        try:
            self.client.send_text(
                to_user,
                text,
                agent_id=int(self.settings.agent_id or 0),
            )
            log.info("已回复 %s: %s", to_user, text.replace("\n", " / ")[:120])
        except Exception:  # noqa: BLE001 - notifications must never fail the job
            log.warning("回复企业微信用户失败（不影响入库）: %s", to_user, exc_info=True)

    def invoice_result(self, job_data: dict[str, Any], outcome: dict[str, Any]) -> None:
        if not self.settings.notify_results:
            return

        invoices = outcome.get("invoices") or []
        duplicates = outcome.get("duplicates") or []
        failed = outcome.get("failed_segments") or []
        to_user = str(job_data.get("from_user") or "")

        if not invoices and duplicates:
            first = duplicates[0]
            text = (
                "ℹ️ 这张发票已经录入过了"
                f"（发票号 {first.get('invoice_no') or '未识别'}），未重复入库。"
            )
            self._send(to_user, text)
            return

        if not invoices:
            return

        if len(invoices) == 1:
            record = invoices[0]
            lines = [
                "✅ 发票已识别并入库",
                f"日期：{record.get('invoice_date') or '-'}",
                f"商家：{record.get('vendor') or '-'}",
                f"金额：{record.get('amount_display') or '-'}",
                f"发票号：{record.get('invoice_no') or '未识别'}",
            ]
            if record.get("needs_review"):
                lines.append("状态：需人工复核")
        else:
            lines = [f"✅ 共识别 {len(invoices)} 张发票"]
            for index, record in enumerate(invoices, 1):
                lines.append(
                    f"{index}. {record.get('invoice_date') or '-'} "
                    f"{record.get('vendor') or '-'} "
                    f"{record.get('amount_display') or '-'}"
                )
            totals: dict[str, int] = defaultdict(int)
            for record in invoices:
                totals[str(record.get("currency") or "JPY")] += int(record.get("amount_minor") or 0)
            lines.append(
                "合计：" + " / ".join(format_money(value, code) for code, value in totals.items())
            )

        if invoices and duplicates:
            lines.append(f"ℹ️ 另有 {len(duplicates)} 张已录入过，未重复入库。")
        if failed:
            lines.append(f"⚠️ 有 {len(failed)} 张识别失败，请补拍后重发。")

        self._send(to_user, "\n".join(lines))

    def invoice_failure(self, to_user: str, error: str) -> None:
        if not self.settings.notify_failure:
            return
        log.warning("通知用户识别失败: %s (%s)", to_user, error)
        self._send(
            to_user,
            "❌ 发票识别失败，请重新拍摄后发送。\n"
            "建议：光线充足、发票平铺、四角完整入镜、避免反光。",
        )
