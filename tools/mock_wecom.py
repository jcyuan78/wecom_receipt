"""A tiny mock of the two WeCom endpoints this service calls.

Used by ``tools/demo_local.py`` and handy for manual debugging without a real
enterprise account: point ``WECOM_WECOM_API_BASE`` at it and send encrypted
callbacks.
"""

from __future__ import annotations

import argparse
import base64

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
REPORT_BYTES = "季度报表\nrevenue: 42\n".encode("utf-8")
REPORT_NAME = "2026Q1-报表.txt"


def create_mock_app() -> FastAPI:
    app = FastAPI(title="Mock WeCom API", docs_url=None, redoc_url=None)

    @app.get("/cgi-bin/gettoken")
    def gettoken(corpid: str = "", corpsecret: str = "") -> dict:
        return {
            "errcode": 0,
            "errmsg": "ok",
            "access_token": "MOCK_ACCESS_TOKEN",
            "expires_in": 7200,
        }

    @app.get("/cgi-bin/media/get")
    def media_get(access_token: str = "", media_id: str = ""):
        if media_id == "IMG_DEMO":
            return Response(
                PNG_BYTES,
                media_type="image/png",
                headers={"Content-Disposition": 'attachment; filename="screenshot.png"'},
            )
        if media_id == "FILE_DEMO":
            return Response(
                REPORT_BYTES,
                media_type="text/plain",
                headers={
                    "Content-Disposition": (
                        "attachment; filename*=UTF-8''2026Q1-%E6%8A%A5%E8%A1%A8.txt"
                    )
                },
            )
        return JSONResponse({"errcode": 40007, "errmsg": "invalid media_id"})

    return app


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description="Mock WeCom API server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9001)
    args = parser.parse_args()
    uvicorn.run(create_mock_app(), host=args.host, port=args.port, log_level="info")
