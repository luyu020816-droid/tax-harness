"""Prepare an independent PG16 binary tree from a verified conda-forge artifact.

Does not stop/start PostgreSQL and never writes to the original binary tree.
"""

import argparse
import hashlib
import io
import json
import shutil
import tarfile
import zipfile
from pathlib import Path

import httpx
import zstandard


def prepare(source, target):
    source, target = source.resolve(), target.resolve()
    if source == target or source in target.parents:
        raise ValueError("Use a separate binary directory")
    package = "win-64/pgvector-0.8.6-h2466b09_0.conda"
    with httpx.Client(follow_redirects=True, timeout=90) as client:
        response = client.get("https://api.anaconda.org/package/conda-forge/pgvector")
        response.raise_for_status()
        entry = next(x for x in response.json()["files"] if x["basename"] == package)
        archive = client.get("https://conda.anaconda.org/conda-forge/" + package)
        archive.raise_for_status()
        actual = hashlib.sha256(archive.content).hexdigest()
        if actual != entry["sha256"]:
            raise RuntimeError("Extension package checksum mismatch")
    if not target.exists():
        shutil.copytree(source, target)
    with zipfile.ZipFile(io.BytesIO(archive.content)) as package_zip:
        payload = next(
            n for n in package_zip.namelist() if n.startswith("pkg-") and n.endswith(".tar.zst")
        )
        with zstandard.ZstdDecompressor().stream_reader(
            io.BytesIO(package_zip.read(payload))
        ) as stream:
            with tarfile.open(fileobj=stream, mode="r|") as tar:
                for member in tar:
                    name = member.name.replace("\\", "/")
                    if not member.isfile() or not name.startswith("Library/"):
                        continue
                    relative = Path(name.removeprefix("Library/"))
                    if ".." in relative.parts or relative.is_absolute():
                        raise ValueError("Invalid package path")
                    destination = target / relative
                    if destination.suffix not in (".dll", ".sql", ".control"):
                        continue
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with destination.open("wb") as output:
                        shutil.copyfileobj(tar.extractfile(member), output)
    manifest = {"package": package, "sha256": actual, "source": str(source), "target": str(target)}
    (target / "pgvector-installation.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    print(json.dumps(manifest))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.source, args.target)
