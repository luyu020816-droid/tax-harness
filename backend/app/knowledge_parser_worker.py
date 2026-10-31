"""Private parser child entry point. It cannot make network connections."""

import json
import socket
import sys
from pathlib import Path

from app.errors import AppError
from app.knowledge_parser import extract


def denied(*args, **kwargs):
    raise OSError("Parser network is disabled")


if __name__ == "__main__":
    socket.socket.connect = denied
    socket.socket.connect_ex = denied
    socket.create_connection = denied
    root = Path(sys.argv[1])
    try:
        config = (
            json.loads((root / "ocr.json").read_text(encoding="utf-8"))
            if (root / "ocr.json").exists()
            else None
        )
        result = extract((root / "input").read_bytes(), sys.argv[2], ocr=config)
    except AppError as exc:
        result = {"error": {"code": exc.code, "message": exc.message}}
    except Exception:
        import traceback

        traceback.print_exc()
        result = {
            "error": {
                "code": "DOCUMENT_PARSE_FAILED",
                "message": "文件解析失败，请检查格式或转换为文本资料。",
            }
        }
    (root / "output.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
