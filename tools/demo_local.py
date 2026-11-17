"""End-to-end local demo: no WeCom account required.

Starts
  1. a mock WeCom API (gettoken + media/get),
  2. the real receiver on a local port,
  3. sends two *encrypted* callbacks (an image and a file) exactly like WeCom
     would, then waits for the files to be downloaded and the jobs to be written.

Run:
    .venv\\Scripts\\python.exe tools\\demo_local.py

Everything is written to ``data/demo/``.
"""

from __future__ import annotations

import base64
import json
import shutil
import socket
import sys
import threading
import time
from pathlib import Path

import httpx
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

from app import xmlmsg  # noqa: E402
from app.config import Settings  # noqa: E402
from app.crypto import WeComCrypto  # noqa: E402
from app.main import create_app  # noqa: E402
from mock_wecom import create_mock_app  # noqa: E402

DEMO_CORP = "wwdemocorp000001"
DEMO_AGENT_ID = "1000002"
DEMO_TOKEN = "demo-callback-token"
DEMO_AES_KEY = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii").rstrip("=")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def start_server(app, port: int) -> tuple[uvicorn.Server, threading.Thread]:
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name=f"uvicorn-{port}", daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            return server, thread
        time.sleep(0.05)
    raise RuntimeError(f"服务未能启动（端口 {port}）")


def callback_xml(msg_id: str, msg_type: str, media_id: str, from_user: str, extra: str = "") -> str:
    return (
        "<xml>"
        f"<ToUserName><![CDATA[{DEMO_CORP}]]></ToUserName>"
        f"<FromUserName><![CDATA[{from_user}]]></FromUserName>"
        "<CreateTime>1700000000</CreateTime>"
        f"<MsgType><![CDATA[{msg_type}]]></MsgType>"
        f"<MediaId><![CDATA[{media_id}]]></MediaId>"
        f"{extra}"
        f"<MsgId>{msg_id}</MsgId>"
        f"<AgentID>{DEMO_AGENT_ID}</AgentID>"
        "</xml>"
    )


def send_callback(client: httpx.Client, crypto: WeComCrypto, inner_xml: str) -> httpx.Response:
    encrypt = crypto.encrypt(inner_xml)
    timestamp, nonce = "1700000000", "demoNonce1"
    signature = crypto.signature(timestamp, nonce, encrypt)
    body = (
        "<xml>"
        f"<ToUserName><![CDATA[{DEMO_CORP}]]></ToUserName>"
        f"<Encrypt><![CDATA[{encrypt}]]></Encrypt>"
        f"<AgentID><![CDATA[{DEMO_AGENT_ID}]]></AgentID>"
        "</xml>"
    )
    return client.post(
        "/wecom/callback",
        params={"msg_signature": signature, "timestamp": timestamp, "nonce": nonce},
        content=body,
        headers={"Content-Type": "text/xml"},
        timeout=10.0,
    )


def main() -> int:
    demo_dir = ROOT / "data" / "demo"
    shutil.rmtree(demo_dir, ignore_errors=True)

    mock_port, app_port = free_port(), free_port()
    settings = Settings(
        corp_id=DEMO_CORP,
        agent_id=DEMO_AGENT_ID,
        app_secret="demo-secret",
        callback_token=DEMO_TOKEN,
        callback_aes_key=DEMO_AES_KEY,
        data_dir=demo_dir,
        worker_enabled=True,
        worker_poll_seconds=0.2,
        wecom_api_base=f"http://127.0.0.1:{mock_port}",
        processor="noop",
        auto_reply="文件已收到，正在自动处理。",
        log_level="WARNING",
    )

    print(f"[1/4] 启动模拟企业微信 API  http://127.0.0.1:{mock_port}")
    mock_server, _ = start_server(create_mock_app(), mock_port)

    print(f"[2/4] 启动文件接收服务      http://127.0.0.1:{app_port}")
    app = create_app(settings)
    app_server, _ = start_server(app, app_port)

    try:
        crypto = WeComCrypto(DEMO_TOKEN, DEMO_AES_KEY, DEMO_CORP)
        base = f"http://127.0.0.1:{app_port}"

        with httpx.Client(base_url=base) as client:
            print("[3/4] 发送两条加密回调（模拟同事发来图片和文件）")
            image_response = send_callback(
                client, crypto, callback_xml("9001", "image", "IMG_DEMO", "zhangsan")
            )
            file_response = send_callback(
                client,
                crypto,
                callback_xml(
                    "9002",
                    "file",
                    "FILE_DEMO",
                    "lisi",
                    "<FileName><![CDATA[2026Q1-报表.txt]]></FileName><FileSize>64</FileSize>",
                ),
            )

        print(f"      图片回调 HTTP {image_response.status_code}，文件回调 HTTP {file_response.status_code}")

        deadline = time.time() + 20
        while time.time() < deadline:
            stats = app.state.ctx.store.stats()
            if stats.get("done", 0) >= 2:
                break
            time.sleep(0.2)
        stats = app.state.ctx.store.stats()

        print("[4/4] 下载与触发结果")
        print(f"      队列状态: {json.dumps(stats, ensure_ascii=False)}")

        ok = True
        for msg_id in ("9001", "9002"):
            row = app.state.ctx.store.get(msg_id)
            if not row or row["status"] != "done":
                print(f"      ✗ 消息 {msg_id} 未完成: {row and row['status']} {row and row['error']}")
                ok = False
                continue
            path = Path(row["file_path"])
            job_path = settings.jobs_dir / f"{msg_id}.json"
            print(f"      ✓ {row['msg_type']:<5} {path.relative_to(ROOT)}  ({row['file_size']} bytes)")
            print(f"        任务文件 {job_path.relative_to(ROOT)}")

        # Verify the passive reply really decrypts with our AES key.
        fields = xmlmsg._parse_fields(image_response.text)
        reply_text = crypto.decrypt(fields["Encrypt"])
        print(f"      自动回复（解密后）: {reply_text}")

        if ok:
            print("\n演示成功。下一步接入真实企业微信，见 docs/wecom-backend-setup.md")
            return 0
        return 1
    finally:
        app_server.should_exit = True
        mock_server.should_exit = True
        time.sleep(0.3)


if __name__ == "__main__":
    raise SystemExit(main())
