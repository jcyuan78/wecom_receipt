"""排查「企业可信IP」：打印真实出口 IP，并用 .env 凭据试调 gettoken。

用法（必须在**实际运行本服务的机器**上执行）：

    .venv\\Scripts\\python.exe tools\\check_egress.py

Cloudflare 反向代理/隧道只影响入站，不影响这里查到的出站 IP。
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

from app.client import WeComAPIError, WeComClient  # noqa: E402
from app.config import Settings  # noqa: E402


def show_proxy_env() -> None:
    print("== 1. 代理环境变量（有值说明请求可能不是从本机直出） ==")
    found = False
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy"):
        value = os.environ.get(name)
        if value:
            found = True
            print(f"  {name}={value}")
    if not found:
        print("  （未设置，直连）")


def _fetch(url: str, *, family: str) -> httpx.Response:
    """Force IPv4 (``0.0.0.0``) or IPv6 (``::``) source address."""
    transport = httpx.HTTPTransport(local_address="0.0.0.0" if family == "ipv4" else "::")
    with httpx.Client(transport=transport, timeout=10.0, follow_redirects=True) as client:
        return client.get(url)


def show_egress_ip() -> None:
    print("\n== 2. 出口公网 IP ==")
    print("  可信IP 只支持 IPv4，所以重点看 IPv4 这一行：")

    try:
        response = _fetch("https://myip.ipip.net", family="ipv4")
        print(f"  IPv4（myip.ipip.net）: {response.text.strip()}")
    except httpx.HTTPError as exc:
        print(f"  IPv4 查询失败（{exc}）")

    try:
        response = _fetch("https://ifconfig.me/ip", family="ipv4")
        print(f"  IPv4（ifconfig.me）  : {response.text.strip()}")
    except httpx.HTTPError as exc:
        print(f"  IPv4 备用查询失败（{exc}）")

    try:
        response = _fetch("https://ifconfig.me/ip", family="ipv6")
        print(f"  IPv6（仅供参考，不能填）: {response.text.strip()}")
    except httpx.HTTPError:
        print("  IPv6: 无（或不可用）")


def try_gettoken() -> None:
    print("\n== 3. 用 .env 里的凭据试调 gettoken ==")
    settings = Settings()
    if not settings.corp_id or not settings.app_secret:
        print("  未配置 WECOM_CORP_ID / WECOM_APP_SECRET，跳过。")
        return

    client = WeComClient(
        corp_id=settings.corp_id,
        secret=settings.app_secret,
        api_base=settings.wecom_api_base,
        timeout=settings.http_timeout,
        max_file_mb=settings.max_file_mb,
    )
    try:
        token = client.access_token()
        print(f"  成功。access_token 前缀: {token[:8]}…  说明出口 IP 已在可信IP 中。")
    except WeComAPIError as exc:
        print(f"  失败: {exc}")
        match = re.search(r"from ip:\s*([0-9a-fA-F.:]+)", str(exc))
        if match:
            print(f"  >>> 企业微信看到的出口 IP 是 {match.group(1)}，应填入『企业可信IP』。")
        if exc.errcode == 60020:
            print("  >>> 60020 = 出口 IP 不在该应用的可信IP 列表里。")
    finally:
        client.close()


def main() -> int:
    print("提示：请在真正运行本服务的机器上执行，而不是你的办公电脑。\n")
    show_proxy_env()
    show_egress_ip()
    try_gettoken()
    print(
        "\n填法：应用详情 → 企业可信IP → 一行一个 IPv4，英文分号分隔；"
        "不支持网段/通配符，仅 IPv4，每个应用最多 120 个。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
