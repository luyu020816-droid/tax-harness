"""Bounded, restartable IPC for native inference. No DB or chat credentials passed."""

import json
import os
import queue
import subprocess
import sys
import threading

from app.config import ROOT
from app.errors import AppError
from app.values import uid


class WorkerExited(Exception):
    pass


class LocalInferenceProcess:
    def __init__(self, directory, profile_id, *, timeout=120):
        self.directory, self.profile_id, self.timeout = str(directory), profile_id, timeout
        self.lock = threading.RLock()
        self.process, self.messages, self.log = None, None, None
        self.counter, self.restarts = 0, 0

    def _command(self):
        return [sys.executable, "-u", "-m", "app.retrieval_worker", self.directory]

    @staticmethod
    def _reader(stream, messages):
        try:
            for line in stream:
                try:
                    message = json.loads(line)
                    if message.get("bge_rpc") == 1:
                        messages.put(message)
                except (ValueError, AttributeError):
                    continue
        except (OSError, ValueError):
            pass
        finally:
            messages.put(None)

    def _receive(self):
        try:
            value = self.messages.get(timeout=self.timeout)
        except queue.Empty as exc:
            self._stop()
            raise AppError(
                "DEPENDENCY_UNAVAILABLE", "本地推理超时，已保留原文，请稍后重试或按日期查找。", 503
            ) from exc
        if value is None:
            raise WorkerExited
        return value

    def _start(self):
        if self.process and self.process.poll() is None:
            return
        self._stop()
        directory = ROOT / ".local/native-inference"
        directory.mkdir(parents=True, exist_ok=True)
        self.log = (directory / (uid() + ".log")).open("wb")
        allowed = {
            "PATH",
            "SYSTEMROOT",
            "WINDIR",
            "TEMP",
            "TMP",
            "USERPROFILE",
            "LOCALAPPDATA",
            "APPDATA",
            "PYTHONPATH",
            "VIRTUAL_ENV",
            "COMSPEC",
        }
        env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
        env.update(
            PYTHONIOENCODING="utf-8",
            PYTHONUTF8="1",
            PYTHONUNBUFFERED="1",
            HF_HUB_OFFLINE="1",
            TRANSFORMERS_OFFLINE="1",
            TOKENIZERS_PARALLELISM="false",
        )
        self.messages = queue.Queue()
        self.process = subprocess.Popen(
            self._command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.log,
            cwd=ROOT,
            env=env,
            text=True,
            encoding="utf-8",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        threading.Thread(
            target=self._reader, args=(self.process.stdout, self.messages), daemon=True
        ).start()
        ready = self._receive()
        if ready.get("kind") != "ready" or ready.get("profile_id") != self.profile_id:
            raise WorkerExited

    def call(self, operation, arguments):
        with self.lock:
            self.counter += 1
            request_id = self.counter
            for attempt in range(2):
                try:
                    self._start()
                    self.process.stdin.write(
                        json.dumps(
                            {"id": request_id, "operation": operation, **arguments},
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                    self.process.stdin.flush()
                    result = self._receive()
                    if result.get("id") != request_id:
                        raise WorkerExited
                    if not result.get("ok"):
                        code = result.get("code")
                        raise AppError(
                            code
                            if code == "DOCUMENT_SEGMENT_TOO_LARGE"
                            else "DEPENDENCY_UNAVAILABLE",
                            "本地模型未能完成该输入，未截断或伪造结果。",
                            503,
                        )
                    return result["value"]
                except (WorkerExited, BrokenPipeError, OSError):
                    self._stop()
                    self.restarts += 1
                    if attempt:
                        raise AppError(
                            "DEPENDENCY_UNAVAILABLE",
                            "本地推理进程异常，原文仍保留；请稍后重试或按日期查找。",
                            503,
                        ) from None

    def _stop(self):
        process, self.process = self.process, None
        if process:
            if process.poll() is None:
                # Windows venv python.exe can launch a second interpreter.
                # Terminate this owned tree, including its native inference.
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                        timeout=5,
                        check=False,
                    )
                if process.poll() is None:
                    process.kill()
            process.wait(timeout=5)
            if process.stdin:
                process.stdin.close()
            if process.stdout:
                process.stdout.close()
        if self.log:
            self.log.close()
            self.log = None

    def close(self):
        with self.lock:
            self._stop()
