"""Text extraction and structural chunks; no database or model calls."""

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
import zipfile
from io import BytesIO
from pathlib import Path

from app.errors import AppError

PARSER_VERSION = "text-1"
CHUNKER_VERSION = "structure-1"
MEDIA = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".md": "text/markdown",
    ".txt": "text/plain",
}


def text_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def inspect_file(name, content):
    suffix = Path(name).suffix.lower()
    if suffix not in MEDIA or not content:
        raise AppError(
            "DOCUMENT_UNSUPPORTED", "请选择PDF、DOCX、UTF-8 Markdown或TXT文件。", 422
        )
    if suffix == ".pdf" and not content.startswith(b"%PDF-"):
        raise AppError("DOCUMENT_UNSUPPORTED", "文件内容不是有效PDF。", 422)
    if suffix == ".docx":
        try:
            with zipfile.ZipFile(BytesIO(content)) as archive:
                entries = archive.infolist()
                names = {e.filename for e in entries}
                if (
                    "word/document.xml" not in names
                    or len(entries) > 5000
                    or sum(e.file_size for e in entries) > 100 * 1024 * 1024
                    or any(e.flag_bits & 1 for e in entries)
                    or any("vbaproject" in n.lower() or "/embeddings/" in n.lower() for n in names)
                ):
                    raise ValueError
        except (zipfile.BadZipFile, ValueError) as exc:
            raise AppError(
                "DOCUMENT_UNSUPPORTED", "DOCX损坏、加密或包含不支持的嵌入内容。", 422
            ) from exc
    if suffix in (".txt", ".md"):
        try:
            decoded = content.decode("utf-8-sig")
            if "\x00" in decoded:
                raise ValueError
        except (UnicodeError, ValueError) as exc:
            raise AppError(
                "DOCUMENT_UNSUPPORTED", "文本文件需要UTF-8编码，不能按二进制内容接入。", 422
            ) from exc
    return suffix, MEDIA[suffix]


def extract(content, suffix, ocr=None):
    blocks = []

    def add(text, kind, heading, locator):
        if text.strip():
            blocks.append(
                {
                    "ordinal": len(blocks),
                    "kind": kind,
                    "heading_path": heading,
                    "content": text,
                    "content_hash": text_hash(text),
                    "locator": locator,
                }
            )

    if suffix in (".md", ".txt"):
        lines = content.decode("utf-8-sig").splitlines(keepends=True)
        heading, start, pending = "", 1, []
        for number, line in enumerate(lines, 1):
            if suffix == ".md" and re.match(r"^#{1,6}\s+", line):
                if pending:
                    add(
                        "".join(pending).rstrip("\r\n"),
                        "paragraph",
                        heading,
                        {"type": "lines", "from": start, "to": number - 1},
                    )
                    pending = []
                heading = line.strip()
            if not line.strip():
                if pending:
                    add(
                        "".join(pending).rstrip("\r\n"),
                        "paragraph",
                        heading,
                        {"type": "lines", "from": start, "to": number - 1},
                    )
                    pending = []
                continue
            if not pending:
                start = number
            pending.append(line)
        if pending:
            add(
                "".join(pending).rstrip("\r\n"),
                "paragraph",
                heading,
                {"type": "lines", "from": start, "to": len(lines)},
            )
    elif suffix == ".pdf":
        if ocr:
            from app.ocr_pdf import extract_pdf

            result = extract_pdf(content, ocr)
            if sum(len(b["content"]) for b in result["blocks"]) > 4_000_000:
                raise AppError("DOCUMENT_TOO_LARGE", "正文超过400万字符，请拆分资料。", 413)
            return result
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(content), strict=True)
        if reader.is_encrypted:
            raise AppError("DOCUMENT_PARSE_FAILED", "加密PDF暂不能接入。", 422)
        if len(reader.pages) > 200:
            raise AppError("DOCUMENT_TOO_LARGE", "文本PDF最多200页。", 413)
        for number, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ""
            if not text.strip() and (page.get_contents() or len(page.images)):
                raise AppError(
                    "OCR_REQUIRED", f"第{number}页没有可提取正文，需要OCR；本版本未发布。", 422
                )
            add(text, "page", "", {"type": "pdf", "page": number, "unit": "physical_page"})
    elif suffix == ".docx":
        from docx import Document
        from docx.table import Table

        document = Document(BytesIO(content))
        heading, paragraph, table = "", 0, 0
        for item in document.iter_inner_content():
            if isinstance(item, Table):
                table += 1
                header = [c.text for c in item.rows[0].cells] if item.rows else []
                for row_number, row in enumerate(item.rows, 1):
                    cells = [c.text for c in row.cells]
                    text = (
                        "\t".join(cells)
                        if row_number == 1
                        else "\t".join(header) + "\n" + "\t".join(cells)
                    )
                    add(
                        text,
                        "table_row",
                        heading,
                        {
                            "type": "docx_table",
                            "table": table,
                            "row": row_number,
                            "includes_header": row_number > 1,
                        },
                    )
            else:
                paragraph += 1
                if item.style and re.match(r"Heading [1-6]|Title|标题", item.style.name):
                    heading = item.text
                add(
                    item.text,
                    "paragraph",
                    heading,
                    {"type": "docx_paragraph", "paragraph": paragraph},
                )
    if not blocks:
        raise AppError("DOCUMENT_PARSE_FAILED", "文件没有可用正文，未发布空知识版本。", 422)
    if sum(len(b["content"]) for b in blocks) > 4_000_000:
        raise AppError("DOCUMENT_TOO_LARGE", "正文超过400万字符，请拆分资料。", 413)
    return {"parser_version": PARSER_VERSION, "blocks": blocks}


def parse_isolated(content, suffix, timeout=60, *, ocr=None):
    """Use a killable child; Windows also caps child memory via a Job Object."""
    with tempfile.TemporaryDirectory(prefix="tax-knowledge-") as directory:
        root = Path(directory)
        (root / "input").write_bytes(content)
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
        if ocr:
            (root / "ocr.json").write_text(json.dumps(ocr), encoding="utf-8")
        # The OCR interpreter deliberately has no editable backend installation.
        # Resolve this project's parser independently of the caller's cwd/env.
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        env.update(
            PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK="1",
            HF_HUB_OFFLINE="1",
            TRANSFORMERS_OFFLINE="1",
            PADDLE_PDX_CACHE_HOME=str(Path(__file__).resolve().parents[2] / ".local/ocr-cache"),
        )
        diagnostic = root / "stderr.log"
        with diagnostic.open("wb") as stderr:
            process = subprocess.Popen(
                [
                    ocr["python"] if ocr else sys.executable,
                    "-m",
                    "app.knowledge_parser_worker",
                    str(root),
                    suffix,
                ],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=stderr,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        job = _limit_windows(process, 6144 if ocr else 512) if os.name == "nt" else None
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            _stop_parser(process)
            raise AppError("DOCUMENT_PARSE_FAILED", "解析超时，请拆分或检查文件。", 422) from exc
        finally:
            if job:
                import ctypes

                ctypes.windll.kernel32.CloseHandle(job)
        output = root / "output.json"

        def details():
            key = uuid.uuid4().hex
            directory = Path(__file__).resolve().parents[2] / ".local/parser-diagnostics"
            directory.mkdir(parents=True, exist_ok=True)
            (directory / (key + ".log")).write_bytes(diagnostic.read_bytes()[-65536:])
            return {"diagnostic_id": key, "parser_exit_code": process.returncode}

        if process.returncode or not output.exists() or output.stat().st_size > 24 * 1024 * 1024:
            raise AppError(
                "DOCUMENT_PARSE_FAILED", "解析进程未完成或超过资源上限。", 422, details=details()
            )
        result = json.loads(output.read_text(encoding="utf-8"))
        if result.get("error"):
            error = result["error"]
            raise AppError(error["code"], error["message"], 422, details=details())
        return result


def _stop_parser(process):
    if process.poll() is None:
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


def _limit_windows(process, memory_mb=512):
    import ctypes as c
    from ctypes import wintypes as w

    class Basic(c.Structure):
        _fields_ = [
            ("user", c.c_int64),
            ("kernel", c.c_int64),
            ("flags", w.DWORD),
            ("minimum", c.c_size_t),
            ("maximum", c.c_size_t),
            ("active", w.DWORD),
            ("affinity", c.c_size_t),
            ("priority", w.DWORD),
            ("scheduling", w.DWORD),
        ]

    class Extended(c.Structure):
        _fields_ = [
            ("basic", Basic),
            ("io", c.c_uint64 * 6),
            ("process_memory", c.c_size_t),
            ("job_memory", c.c_size_t),
            ("peak_process", c.c_size_t),
            ("peak_job", c.c_size_t),
        ]

    api = c.windll.kernel32
    api.CreateJobObjectW.restype = w.HANDLE
    api.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
    api.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    api.CloseHandle.argtypes = [w.HANDLE]
    handle = api.CreateJobObjectW(None, None)
    limits = Extended()
    limits.basic.flags = 0x100 | 0x200 | 0x2000  # PROCESS_MEMORY | JOB_MEMORY | KILL_ON_JOB_CLOSE
    limits.process_memory = memory_mb * 1024 * 1024
    limits.job_memory = memory_mb * 1024 * 1024
    if (
        not handle
        or not api.SetInformationJobObject(handle, 9, c.byref(limits), c.sizeof(limits))
        or not api.AssignProcessToJobObject(handle, int(process._handle))
    ):
        _stop_parser(process)
        if handle:
            api.CloseHandle(handle)
        raise AppError("DEPENDENCY_UNAVAILABLE", "解析隔离资源限制不可用，未执行无约束解析。", 503)
    return handle


def chunk_blocks(blocks, title, token_count):
    parts = []
    for block in blocks:
        text = block["content"]
        boundaries = [m.end() for m in re.finditer(r"[。！？；;]|\n{2,}", text)] + [len(text)]
        start, end = 0, 0
        for boundary in sorted(set(boundaries)):
            if token_count(text[start:boundary]) > 480 and end > start:
                parts.append(
                    {
                        **block,
                        "content": text[start:end],
                        "locator": {**block["locator"], "char_start": start, "char_end": end},
                    }
                )
                start = end
            end = boundary
            if token_count(title + "\n" + block["heading_path"] + "\n" + text[start:end]) > 1200:
                raise AppError(
                    "DOCUMENT_SEGMENT_TOO_LARGE",
                    "单条款或表格行超过输入上限，请按完整条款拆分，未截断原文。",
                    422,
                )
        if end > start:
            parts.append(
                {
                    **block,
                    "content": text[start:end],
                    "locator": {**block["locator"], "char_start": start, "char_end": end},
                }
            )
    chunks, pending = [], []

    def emit():
        content = "\n\n".join(p["content"] for p in pending)
        retrieval = title + "\n" + pending[0]["heading_path"] + "\n" + content
        locators = []
        for part in pending:
            locator = part["locator"]
            if locator.get("extraction") != "ocr":
                if locator not in locators:
                    locators.append(locator)
                continue
            # Keep full per-block provenance in knowledge_blocks. Citations
            # carry one enclosing rectangle per page, avoiding repeated model
            # hashes consuming the evidence budget before any text is returned.
            page = next(
                (
                    p
                    for p in locators
                    if p.get("extraction") == "ocr" and p["page"] == locator["page"]
                ),
                None,
            )
            if page is None:
                page = {
                    k: locator[k]
                    for k in (
                        "type",
                        "page",
                        "unit",
                        "bbox",
                        "bbox_unit",
                        "origin",
                        "extraction",
                        "ocr_version",
                        "confidence_min",
                        "confidence_mean",
                        "normalization",
                    )
                    if k in locator
                }
                page = {
                    **page,
                    "bbox": list(page["bbox"]),
                    "bbox_kind": "enclosing_blocks",
                    "layout_blocks": [],
                }
                locators.append(page)
            box = page["bbox"]
            other = locator["bbox"]
            page["bbox"] = [
                min(box[0], other[0]),
                min(box[1], other[1]),
                max(box[2], other[2]),
                max(box[3], other[3]),
            ]
            page["confidence_min"] = min(page["confidence_min"], locator["confidence_min"])
            if locator["layout_block"] not in page["layout_blocks"]:
                page["layout_blocks"].append(locator["layout_block"])
        chunks.append(
            {
                "ordinal": len(chunks),
                "content": content,
                "retrieval_text": retrieval,
                "block_ordinals": list(dict.fromkeys(p["ordinal"] for p in pending)),
                "locator": locators,
                "token_count": token_count(retrieval),
                "content_hash": text_hash(content),
            }
        )

    for part in parts:
        combined = (
            title
            + "\n"
            + part["heading_path"]
            + "\n"
            + "\n\n".join(p["content"] for p in [*pending, part])
        )
        if pending and (
            token_count(combined) > 900 or pending[-1]["heading_path"] != part["heading_path"]
        ):
            emit()
            overlap = pending[-1:]
            pending = (
                overlap
                if token_count(overlap[0]["content"]) <= 80
                and overlap[0]["heading_path"] == part["heading_path"]
                else []
            )
            if (
                pending
                and token_count(
                    title
                    + "\n"
                    + part["heading_path"]
                    + "\n"
                    + pending[0]["content"]
                    + "\n\n"
                    + part["content"]
                )
                > 1200
            ):
                pending = []
        pending.append(part)
    if pending:
        emit()
    if len(chunks) > 2000:
        raise AppError("DOCUMENT_TOO_LARGE", "单版本超过2000个片段，请拆分资料。", 413)
    return chunks
