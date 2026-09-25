#!/usr/bin/env python3
"""Create clean ZIP distribution package for FantaManager."""
import os
import shutil
import sys
import zipfile
from pathlib import Path

BASE_DIR = Path("/Users/luca/Desktop/asta")

EXCLUDE_DIR_NAMES = {
    ".venv-mac",
    ".venv",
    "venv",
    ".git",
    "build",
    "dist",
    "__pycache__",
    ".pytest_cache",
    ".idea",
    ".vscode",
}

EXCLUDE_EXTS = {
    ".pyc",
    ".pyo",
    ".pyd",
    ".DS_Store",
    ".shm",
    ".wal",
}

EXCLUDE_FILENAMES = {
    ".DS_Store",
    "server.log",
}


def create_package():
    # 1. Sync latest sqlite to data/db.sqlite3 for docker/nas mount
    src_db = BASE_DIR / "db.sqlite3"
    dst_db = BASE_DIR / "data" / "db.sqlite3"
    if src_db.exists():
        dst_db.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_db, dst_db)
        print(f"✓ Copiato database aggiornato in {dst_db.relative_to(BASE_DIR)}")

    # Target zip files
    targets = [
        BASE_DIR / "fantamanager.zip",
        BASE_DIR / "fantamanager-synology.zip",
    ]

    # Collect files
    files_to_zip = []
    for root, dirs, files in os.walk(BASE_DIR):
        rel_root = Path(root).relative_to(BASE_DIR)

        # Exclude directories
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIR_NAMES and not any(part in EXCLUDE_DIR_NAMES for part in (rel_root / d).parts)]

        for f in files:
            if f.endswith(".zip"):
                continue
            if f in EXCLUDE_FILENAMES:
                continue
            suffix = Path(f).suffix.lower()
            if suffix in EXCLUDE_EXTS:
                continue

            file_path = Path(root) / f
            arc_name = file_path.relative_to(BASE_DIR)
            files_to_zip.append((file_path, str(arc_name)))

    files_to_zip.sort(key=lambda x: x[1])

    for target_zip in targets:
        print(f"\n📦 Creazione pacchetto: {target_zip.name} ...")
        # Temporary zip to ensure atomic write
        tmp_zip = target_zip.with_suffix(".tmp")
        with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as z:
            for file_path, arc_name in files_to_zip:
                z.write(file_path, arc_name)

        if target_zip.exists():
            target_zip.unlink()
        tmp_zip.rename(target_zip)

        size_mb = target_zip.stat().st_size / (1024 * 1024)
        print(f"✓ {target_zip.name}: {len(files_to_zip)} file inclusi ({size_mb:.2f} MB)")

    print("\n✅ Tutti i pacchetti ZIP sono stati creati con successo!")


if __name__ == "__main__":
    create_package()
