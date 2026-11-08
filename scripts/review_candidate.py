"""Record a review candidate without reading credentials or querying a database."""

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_TREES = (
    "backend/app",
    "backend/migrations",
    "backend/tests",
    "frontend/src",
    "frontend/tests",
    "scripts",
)
SOURCE_SUFFIXES = {".py", ".sql", ".ts", ".tsx", ".css", ".ps1"}
ROOT_FILES = (
    "README.md",
    "backend/pyproject.toml",
    "backend/requirements.lock.txt",
    "frontend/package.json",
    "frontend/package-lock.json",
    "frontend/vite.config.ts",
    "frontend/playwright.config.ts",
    "frontend/tsconfig.json",
    "frontend/index.html",
    "frontend/showcase.html",
)


def hashes(paths):
    return {
        p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(set(paths))
        if p.is_file() and not p.is_symlink()
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    paths = [ROOT / name for name in ROOT_FILES]
    for directory in SOURCE_TREES:
        paths.extend(
            p
            for p in (ROOT / directory).rglob("*")
            if p.suffix in SOURCE_SUFFIXES
            and not any(
                part.startswith(".") or part == "__pycache__"
                for part in p.relative_to(ROOT).parts
            )
        )
    sources = hashes(paths)
    candidate = hashlib.sha256(
        json.dumps(sources, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    build = hashes((ROOT / "frontend/dist").rglob("*"))
    record = {
        "recorded_at": datetime.now(UTC).isoformat(),
        "candidate_sha256": candidate,
        "source_files": sources,
        "frontend_build_files": build,
        "validation_status": "not_asserted",
        "limitations": [
            "Fingerprint only; does not lock files or certify test success.",
            "Credentials, model configuration, databases and private evidence are excluded.",
            "Model identity and test dataset version must be recorded separately without secrets.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(
        f"Candidate {candidate}; {len(sources)} source files, {len(build)} build files."
    )
    print(f"Manifest: {args.output.resolve()}")


if __name__ == "__main__":
    main()
