"""Install the pinned official portable Windows runtime inside this project."""

import hashlib
import shutil
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
URL = "https://get.enterprisedb.com/postgresql/postgresql-16.15-1-windows-x64-binaries.zip"
SHA256 = "25e6fcdfb8caec38691bf461125e7564508760666f7b8e5dc6a5f0818f58f81e"


def main():
    tools = ROOT / ".tools"
    if (tools / "pgsql/bin/pg_ctl.exe").exists():
        print("Existing project PostgreSQL runtime kept.")
        return
    tools.mkdir(exist_ok=True)
    archive = tools / "postgresql-16.15-windows-x64.zip"
    if not archive.exists():
        temporary = archive.with_suffix(".part")
        with (
            urllib.request.urlopen(URL, timeout=60) as response,
            temporary.open("wb") as output,
        ):
            shutil.copyfileobj(response, output)
        temporary.replace(archive)
    with archive.open("rb") as source:
        checksum = hashlib.file_digest(source, "sha256").hexdigest()
    if checksum != SHA256:
        raise SystemExit("PostgreSQL archive checksum mismatch; no files extracted.")
    with zipfile.ZipFile(archive) as contents:
        for item in contents.infolist():
            if not item.filename.startswith(
                ("pgsql/bin/", "pgsql/lib/", "pgsql/share/")
            ):
                continue
            destination = (tools / item.filename).resolve()
            if not destination.is_relative_to(tools.resolve()):
                raise SystemExit("Archive contains an unsafe path.")
            contents.extract(item, tools)
    print("Official PostgreSQL 16.15 runtime installed inside .tools/pgsql.")


if __name__ == "__main__":
    main()
