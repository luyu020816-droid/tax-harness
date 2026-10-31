"""Pinned local dense embedding, lexical terms and optional cross-encoder ranking."""

import gc
import hashlib
import importlib.metadata
import importlib.util
import json
import math
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

from app.errors import AppError
from app.values import digest

LEXICAL_VERSION = "jieba-0.42.1-hmm-off-v1"
STOP = set(
    "的 了 吗 呢 是 在 和 与 及 请 请问 怎么 如何 什么 哪些 有没有 一下 帮我 查询 查找 相关 资料 规定 要求 企业 公司 甲 乙 是否 需要".split()
)
_lexical = None
_torch_setup_lock = threading.Lock()
_torch_configured = False


def terms(text):
    global _lexical
    if _lexical is None:
        import jieba

        jieba.setLogLevel(40)
        _lexical = jieba.Tokenizer()
    words = [
        w.lower()
        for w in _lexical.cut(text, HMM=False)
        if re.fullmatch(r"[A-Za-z0-9_\u4e00-\u9fff]+", w)
    ]
    return list(
        dict.fromkeys(
            w.lower()
            for w in [
                *re.findall(r"(?<![A-Za-z0-9])[CFR]\d{3,12}(?![A-Za-z0-9])", text, re.I),
                *words,
            ]
            if w not in STOP
        )
    )


def dictionary_hash():
    import jieba

    with jieba.get_dict_file() as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def vector_literal(vector):
    if len(vector) != 1024 or not all(math.isfinite(v) for v in vector):
        raise AppError("DEPENDENCY_UNAVAILABLE", "向量维度或数值不符合当前索引空间。", 503)
    norm = math.sqrt(sum(v * v for v in vector))
    if not 0.99 < norm < 1.01:
        raise AppError("DEPENDENCY_UNAVAILABLE", "向量没有按当前profile归一化。", 503)
    return json.dumps(vector, separators=(",", ":"), allow_nan=False)


class LocalRetrieval:
    def __init__(self, directory, *, isolated=True):
        self.directory = Path(directory)
        self.lock = threading.RLock()
        self.installation = None
        self.tokenizers = {}
        self.model, self.model_kind = None, None
        self.isolated, self.process = isolated, None
        # Native CPU models retain thread-local runtime state. All loads and
        # inference use this one worker, even when API/job callers change threads.
        self.inference_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bge-inference")

    def installed(self):
        missing = [
            name
            for name in ("torch", "transformers", "jieba", "pypdf")
            if importlib.util.find_spec(name) is None
        ]
        manifest = self.directory / "installation.json"
        return {
            "dependencies": not missing,
            "missing": missing,
            "manifest_present": manifest.exists(),
        }

    def _verify(self):
        if self.installation:
            return self.installation
        try:
            data = json.loads((self.directory / "installation.json").read_text(encoding="utf-8"))
            for kind in ("embedding", "reranker"):
                model = data["models"][kind]
                if not re.fullmatch("[a-f0-9]{40}", model["revision"]):
                    raise ValueError
                for name, meta in model["files"].items():
                    path = (self.directory / model["directory"] / name).resolve()
                    if (
                        not path.is_relative_to(self.directory.resolve())
                        or path.stat().st_size != meta["bytes"]
                    ):
                        raise ValueError
                    with path.open("rb") as stream:
                        if hashlib.file_digest(stream, "sha256").hexdigest() != meta["sha256"]:
                            raise ValueError
            self.installation = data
        except (OSError, KeyError, ValueError) as exc:
            raise AppError(
                "DEPENDENCY_UNAVAILABLE",
                "本地检索权重或安装校验清单不完整，知识索引暂不可用。",
                503,
            ) from exc
        return data

    def _tokenizer(self, kind):
        self._verify()
        if kind not in self.tokenizers:
            from transformers import AutoTokenizer

            self.tokenizers[kind] = AutoTokenizer.from_pretrained(
                self.directory / self.installation["models"][kind]["directory"],
                local_files_only=True,
                trust_remote_code=False,
            )
        return self.tokenizers[kind]

    def count(self, text):
        if self.isolated:
            return self._remote("count", {"text": text})
        with self.lock:
            return len(
                self._tokenizer("embedding").encode(text, add_special_tokens=True, truncation=False)
            )

    def manifest(self):
        with self.lock:
            data = self._verify()
            return {
                "models": data["models"],
                "dimension": 1024,
                "normalization": "l2-float32-cls",
                "input_limit": 8192,
                "chunk_limit": 1200,
                "lexical": LEXICAL_VERSION,
                "dictionary_sha256": dictionary_hash(),
                "stopwords_sha256": digest(sorted(STOP)),
                "dependencies": {
                    n: importlib.metadata.version(n)
                    for n in ("torch", "transformers", "tokenizers", "jieba", "numpy")
                },
            }

    def profile(self):
        manifest = self.manifest()
        return digest(manifest), manifest

    def _load(self, kind):
        global _torch_configured
        if self.model_kind != kind:
            self.model = None
            gc.collect()
            import torch
            from transformers import AutoModel, AutoModelForSequenceClassification

            with _torch_setup_lock:
                if not _torch_configured:
                    torch.set_num_threads(4)
                    _torch_configured = True
            factory = AutoModel if kind == "embedding" else AutoModelForSequenceClassification
            self.model = factory.from_pretrained(
                self.directory / self.installation["models"][kind]["directory"],
                local_files_only=True,
                trust_remote_code=False,
                weights_only=True,
            ).eval()
            self.model_kind = kind
        return self.model

    def embed(self, texts):
        if self.isolated:
            vectors = self._remote("embed", {"texts": texts})
            if len(vectors) != len(texts):
                raise AppError("DEPENDENCY_UNAVAILABLE", "本地向量结果不完整。", 503)
            for vector in vectors:
                vector_literal(vector)
            return vectors
        return self.inference_pool.submit(self._embed, texts).result()

    def _embed(self, texts):
        with self.lock:
            import torch

            tokenizer = self._tokenizer("embedding")
            lengths = [
                len(tokenizer.encode(t, add_special_tokens=True, truncation=False)) for t in texts
            ]
            if any(n > 8192 for n in lengths):
                raise AppError(
                    "DOCUMENT_SEGMENT_TOO_LARGE", "检索输入超出真实tokenizer上限，未静默截断。", 422
                )
            model = self._load("embedding")
            vectors = []
            with torch.inference_mode():
                for start in range(0, len(texts), 2):
                    inputs = tokenizer(
                        texts[start : start + 2],
                        padding=True,
                        truncation=False,
                        return_tensors="pt",
                    )
                    outputs = model(**inputs).last_hidden_state[:, 0]
                    result = (
                        torch.nn.functional.normalize(outputs.float(), p=2, dim=1).cpu().tolist()
                    )
                    for vector in result:
                        vector_literal(vector)
                    vectors.extend(result)
            return vectors

    def rerank(self, query, passages):
        if self.isolated:
            values = self._remote("rerank", {"query": query, "passages": passages})
            if len(values) != len(passages) or not all(math.isfinite(v) for v in values):
                raise AppError("DEPENDENCY_UNAVAILABLE", "本地重排结果不完整。", 503)
            return values
        return self.inference_pool.submit(self._rerank, query, passages).result()

    def _rerank(self, query, passages):
        with self.lock:
            import torch

            tokenizer = self._tokenizer("reranker")
            pairs = [[query, passage] for passage in passages]
            if any(
                len(tokenizer.encode(*p, add_special_tokens=True, truncation=False)) > 8192
                for p in pairs
            ):
                raise AppError(
                    "DOCUMENT_SEGMENT_TOO_LARGE", "重排输入超出模型上限，未截断证据。", 422
                )
            model = self._load("reranker")
            scores = []
            with torch.inference_mode():
                for start in range(0, len(pairs), 2):
                    inputs = tokenizer(
                        pairs[start : start + 2],
                        padding=True,
                        truncation=False,
                        return_tensors="pt",
                    )
                    scores.extend(model(**inputs).logits.reshape(-1).float().cpu().tolist())
            if len(scores) != len(passages) or not all(math.isfinite(s) for s in scores):
                raise AppError("DEPENDENCY_UNAVAILABLE", "重排未返回完整有效结果。", 503)
            return scores

    def close(self):
        if self.process:
            self.process.close()
        self.inference_pool.shutdown(wait=True, cancel_futures=True)

    @contextmanager
    def external_work(self):
        """Release cached native weights while a bounded OCR job uses the CPU."""
        with self.lock:
            if self.process:
                self.process.close()
                self.process = None
            yield

    def _remote(self, operation, arguments):
        from app.retrieval_process import LocalInferenceProcess

        with self.lock:
            if self.process is None:
                self.process = LocalInferenceProcess(self.directory, self.profile()[0])
            return self.process.call(operation, arguments)
