"""Generate the two secrets you paste into the WeCom admin console."""

from __future__ import annotations

import base64
import secrets


def main() -> None:
    token = secrets.token_urlsafe(18)
    aes_key = base64.b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")
    print("Token (复制到后台「Token」):")
    print(f"  {token}")
    print("EncodingAESKey (复制到后台「EncodingAESKey」):")
    print(f"  {aes_key}")
    print("\n把这两个值同时写入 .env 的 WECOM_CALLBACK_TOKEN / WECOM_CALLBACK_AES_KEY。")


if __name__ == "__main__":
    main()
