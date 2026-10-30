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
    socket.socket = denied
    root = Path(sys.argv[1])
    try:
        result = extract((root / "input").read_bytes(), sys.argv[2])
    except AppError as exc:
        result = {"error": {"code": exc.code, "message": exc.message}}
    except Exception:
        result = {
            "error": {
                "code": "DOCUMENT_PARSE_FAILED",
                "message": "文件解析失败，请检查格式或转换为文本资料。",
            }
        }
    (root / "output.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
