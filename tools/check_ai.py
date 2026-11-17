"""Check that Ollama and the configured vision model are ready.

Usage:
    .venv\\Scripts\\python.exe tools\\check_ai.py
    .venv\\Scripts\\python.exe tools\\check_ai.py --image data\\receipt\\2026-01\\xxx.jpg
"""

from __future__ import annotations

import argparse
import json
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

from app.classifier import Classifier  # noqa: E402
from app.config import Settings  # noqa: E402
from app.imaging import load_image  # noqa: E402

from app.ollama import OllamaClient, OllamaError  # noqa: E402

VISION_HINTS = ("vl", "vision", "llava", "minicpm-v", "moondream", "gemma3")


def main() -> int:
    parser = argparse.ArgumentParser(description="检查 Ollama 与视觉模型")
    parser.add_argument("--image", type=Path, help="可选：用一张真实图片跑分类/解析")
    args = parser.parse_args()

    settings = Settings()
    print(f"Ollama 地址: {settings.ollama_base_url}")
    print(f"配置模型  : {settings.ollama_model}")

    client = OllamaClient(
        base_url=settings.ollama_base_url,
        model=settings.ollama_model,
        timeout=settings.ollama_timeout,
        keep_alive=settings.ollama_keep_alive,
    )

    try:
        models = client.list_models()
    except OllamaError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        print("  排查：ollama serve 是否在运行？地址是否正确？", file=sys.stderr)
        return 1

    print(f"已安装模型: {', '.join(models) if models else '（无）'}")
    if settings.ollama_model not in models:
        print(
            f"⚠ 未找到 {settings.ollama_model}，请执行: ollama pull {settings.ollama_model}"
        )

    name = settings.ollama_model.lower()
    if not any(hint in name for hint in VISION_HINTS):
        print(
            "⚠ 该模型名不像视觉模型。qwen2.5 / qwen3 的纯文本版本不能读图；"
            "请使用 qwen2.5vl:7b、qwen3-vl 或 llava 等视觉模型。"
        )

    if not args.image:
        print("\n加 --image <路径> 可以用真实发票测试分类与字段提取。")
        client.close()
        return 0

    image = load_image(args.image)
    print(f"\n测试图片: {args.image} {image.size}")
    try:
        analysis = Classifier(client).analyze(
            image,
            received_date=date.today(),
            default_currency=settings.default_currency,
        )
        print(f"分类: {analysis.category} ({analysis.confidence:.2f}) {analysis.reason}")
        if analysis.fields is not None:
            print(json.dumps(analysis.fields.to_public_dict(), ensure_ascii=False, indent=2))
    except OllamaError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
