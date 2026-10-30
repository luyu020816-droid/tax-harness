"""Private local inference worker; stdout is a JSON protocol, never user content logs."""

import ctypes
import faulthandler
import json
import os
import sys

from app.errors import AppError
from app.retrieval_engine import LocalRetrieval


def main():
    if sys.platform == "win32":
        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002)
    faulthandler.enable()
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", buffering=1)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    engine = LocalRetrieval(sys.argv[1], isolated=False)
    profile, _ = engine.profile()

    def send(value):
        protocol.write(
            json.dumps({"bge_rpc": 1, **value}, ensure_ascii=False, allow_nan=False) + "\n"
        )
        protocol.flush()

    send({"kind": "ready", "profile_id": profile})
    for line in sys.stdin:
        request = json.loads(line)
        try:
            if request["operation"] == "count":
                value = engine.count(request["text"])
            elif request["operation"] == "embed":
                value = engine._embed(request["texts"])
            elif request["operation"] == "rerank":
                value = engine._rerank(request["query"], request["passages"])
            else:
                raise AppError("DEPENDENCY_UNAVAILABLE", "Unknown inference operation")
            send({"id": request["id"], "ok": True, "value": value})
        except Exception as error:
            send(
                {
                    "id": request["id"],
                    "ok": False,
                    "code": error.code if isinstance(error, AppError) else "DEPENDENCY_UNAVAILABLE",
                }
            )


if __name__ == "__main__":
    main()
