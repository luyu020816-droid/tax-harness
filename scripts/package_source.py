"""Build a small source-only archive from an explicit allowlist."""

import argparse
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_ROOT_FILES = {"README.md", ".gitignore", ".env.example", "compose.yaml"}
ALLOWED_DIRECTORIES = {"backend", "frontend", "scripts", "docs", "examples", "evals"}
EXCLUDED_PARTS = {
    "node_modules",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "test-results",
    "playwright-report",
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(args.output, "x", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(ROOT.rglob("*")):
            relative = path.relative_to(ROOT)
            if not path.is_file() or any(
                part in EXCLUDED_PARTS or part.endswith(".egg-info")
                for part in relative.parts
            ):
                continue
            if (
                len(relative.parts) == 1 and relative.name in ALLOWED_ROOT_FILES
            ) or relative.parts[0] in ALLOWED_DIRECTORIES:
                if path.suffix in (".log", ".pyc") or relative.name in (
                    ".env",
                    ".env.local",
                ):
                    continue
                archive.write(path, relative.as_posix())
                count += 1
    print(
        f"Created source archive with {count} files; local secrets, runtimes and database excluded."
    )


if __name__ == "__main__":
    main()
