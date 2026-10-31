"""Install fixed public Paddle inference data; never fetch executable model code."""

import argparse
import hashlib
import json
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
MODELS = {
    "layout_detection": "PP-DocLayout_plus-L",
    "text_detection": "PP-OCRv5_mobile_det",
    "text_recognition": "PP-OCRv5_mobile_rec",
    "table_classification": "PP-LCNet_x1_0_table_cls",
    "wired_table_structure_recognition": "SLANeXt_wired",
    "wireless_table_structure_recognition": "SLANet_plus",
    "wired_table_cells_detection": "RT-DETR-L_wired_table_cell_det",
    "wireless_table_cells_detection": "RT-DETR-L_wireless_table_cell_det",
    "table_orientation_classify": "PP-LCNet_x1_0_doc_ori",
}
FILES = {"inference.json", "inference.yml", "inference.pdiparams"}


def install(prepare=False):
    lockpath = ROOT / "backend/ocr-models.lock.json"
    target = ROOT / ".local/ocr-models"
    target.mkdir(parents=True, exist_ok=True)
    lock = {"models": {}} if prepare else json.loads(lockpath.read_text(encoding="utf-8"))
    with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(180, connect=20)) as client:
        for role, name in MODELS.items():
            repo = "PaddlePaddle/" + name
            if prepare:
                response = client.get(
                    "https://huggingface.co/api/models/" + repo, params={"blobs": "true"}
                )
                response.raise_for_status()
                info = response.json()
                model = {"repo": repo, "revision": info["sha"], "name": name, "files": {}}
                for entry in info["siblings"]:
                    if entry["rfilename"] in FILES:
                        model["files"][entry["rfilename"]] = {
                            "sha256": entry.get("lfs", {}).get("sha256")
                        }
                lock["models"][role] = model
            model = lock["models"][role]
            directory = target / name
            directory.mkdir(exist_ok=True)
            for filename, meta in model["files"].items():
                path = directory / filename

                def sha(path=path):
                    with path.open("rb") as stream:
                        return hashlib.file_digest(stream, "sha256").hexdigest()

                if not path.exists() or not meta.get("sha256") or sha() != meta["sha256"]:
                    print(name, filename, "downloading", flush=True)
                    part = path.with_suffix(path.suffix + ".partial")
                    with client.stream(
                        "GET",
                        f"https://huggingface.co/{repo}/resolve/{model['revision']}/{filename}",
                    ) as response:
                        response.raise_for_status()
                        with part.open("wb") as out:
                            for block in response.iter_bytes(1024 * 1024):
                                out.write(block)
                    part.replace(path)
                actual = sha()
                if meta.get("sha256") and actual != meta["sha256"]:
                    raise ValueError("OCR weight checksum mismatch")
                meta.update(sha256=actual, bytes=path.stat().st_size)
            if (
                not {"inference.json", "inference.pdiparams", "inference.yml"}
                <= model["files"].keys()
            ):
                raise ValueError("Incomplete Paddle model data")
    if prepare:
        lockpath.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
    (target / "installation.json").write_text(json.dumps(lock, indent=2), encoding="utf-8")
    print("OCR model data verified.", flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--prepare-lock", action="store_true")
    install(p.parse_args().prepare_lock)
