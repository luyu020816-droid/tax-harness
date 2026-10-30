"""Download pinned public model files and record hashes; never download remote code."""

import argparse
import hashlib
import json
from pathlib import Path

import httpx

MODELS = {
    "embedding": ("BAAI/bge-m3", "5617a9f61b028005a4858fdac845db406aefb181", "pytorch_model.bin"),
    "reranker": (
        "BAAI/bge-reranker-v2-m3",
        "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e",
        "model.safetensors",
    ),
}
METADATA = {
    "config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "sentencepiece.bpe.model",
}


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def install(directory):
    directory.mkdir(parents=True, exist_ok=True)
    models = {}
    with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(120, connect=20)) as client:
        for kind, (repo, revision, weight) in MODELS.items():
            info = client.get(
                f"https://huggingface.co/api/models/{repo}/revision/{revision}",
                params={"blobs": "true"},
            )
            info.raise_for_status()
            files = {}
            target = directory / kind
            target.mkdir(exist_ok=True)
            for entry in info.json()["siblings"]:
                name = entry["rfilename"]
                if name not in METADATA | {weight}:
                    continue
                path = target / name
                expected = entry.get("lfs", {}).get("sha256")
                if not path.exists() or (expected and sha256(path) != expected):
                    part = path.with_suffix(path.suffix + ".partial")
                    print(kind, name, "downloading", flush=True)
                    with client.stream(
                        "GET", f"https://huggingface.co/{repo}/resolve/{revision}/{name}"
                    ) as response:
                        response.raise_for_status()
                        size, last = 0, 0
                        with part.open("wb") as output:
                            for chunk in response.iter_bytes(1024 * 1024):
                                output.write(chunk)
                                size += len(chunk)
                                if size - last >= 128 * 1024 * 1024:
                                    print(kind, name, f"{size // (1024 * 1024)} MiB", flush=True)
                                    last = size
                    actual = sha256(part)
                    if expected and actual != expected:
                        raise RuntimeError("Model file hash differs from pinned upstream metadata")
                    part.replace(path)
                files[name] = {"sha256": sha256(path), "bytes": path.stat().st_size}
            models[kind] = {
                "repository": repo,
                "revision": revision,
                "directory": kind,
                "files": files,
            }
            (directory / "installation.json").write_text(
                json.dumps({"format": 1, "models": models}, indent=2), encoding="utf-8"
            )
            print(kind, "files verified; inference validation still required", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, default=Path(".local/retrieval-models"))
    install(parser.parse_args().directory.resolve())
