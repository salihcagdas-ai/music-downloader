"""Build a Windows executable without bundling FFmpeg or a JavaScript runtime."""

from __future__ import annotations

import os
import re
import shutil
from importlib.metadata import distributions
from pathlib import Path


def collect_notices(destination: Path) -> None:
    """Preserve installed distributions' license files alongside the executable."""
    destination.mkdir(parents=True, exist_ok=True)
    inventory = []
    for package in sorted(distributions(), key=lambda item: item.metadata.get("Name", "")):
        name = package.metadata.get("Name", "unknown")
        folder = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{name}-{package.version}")
        license_info = (
            package.metadata.get("License-Expression")
            or package.metadata.get("License")
            or "See the package license files and upstream project."
        )
        inventory.append(f"{name}=={package.version}\n{license_info}\n")
        for index, item in enumerate(package.files or []):
            if not any(word in item.name.lower() for word in ("license", "copying", "notice")):
                continue
            source = Path(package.locate_file(item))
            if source.is_file():
                target = destination / folder / f"{index:04d}-{item.name}"
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
    (destination / "PACKAGES.txt").write_text("\n".join(inventory), encoding="utf-8")


def main() -> None:
    if os.name != "nt":
        raise SystemExit("Build the Windows executable on Windows.")
    import PyInstaller.__main__

    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    collect_notices(root / "dist" / "THIRD_PARTY_NOTICES")
    shutil.copyfile(root / "THIRD_PARTY.md", root / "dist" / "THIRD_PARTY.md")
    PyInstaller.__main__.run(
        [
            "--onefile",
            "--noconsole",
            "--clean",
            "--noconfirm",
            "--name=MusicDownloader",
            f"--add-data={root / 'templates'}{os.pathsep}templates",
            f"--add-data={root / 'static'}{os.pathsep}static",
            "--collect-all=yt_dlp",
            "--collect-all=yt_dlp_ejs",
            "--collect-all=certifi",
            "app.py",
        ]
    )


if __name__ == "__main__":
    main()
