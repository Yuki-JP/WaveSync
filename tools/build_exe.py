"""Build the Windows desktop executable with PyInstaller."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BUILD_REQUIREMENTS = PROJECT_ROOT / "requirements-build.txt"
ENTRYPOINT = PROJECT_ROOT / "tkinter_app.py"


def run(command: list[str]) -> None:
    print(" ".join(f'"{item}"' if " " in item else item for item in command), flush=True)
    subprocess.run(command, cwd=PROJECT_ROOT, check=True)


def ensure_runtime_dependencies() -> None:
    run([sys.executable, "-m", "pip", "install", "-r", str(PROJECT_ROOT / "requirements.txt")])


def ensure_pyinstaller() -> None:
    if importlib.util.find_spec("PyInstaller") is not None:
        return
    run([sys.executable, "-m", "pip", "install", "-r", str(BUILD_REQUIREMENTS)])


def build_exe() -> None:
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--windowed",
        "--name",
        "PluralEyesClone",
        "--workpath",
        str(PROJECT_ROOT / "build" / "pyinstaller"),
        "--specpath",
        str(PROJECT_ROOT / "build"),
        "--distpath",
        str(PROJECT_ROOT / "dist"),
        "--hidden-import",
        "main",
        "--collect-submodules",
        "backend",
        "--collect-submodules",
        "tools",
        "--collect-submodules",
        "imageio_ffmpeg",
        "--collect-data",
        "imageio_ffmpeg",
        "--collect-binaries",
        "imageio_ffmpeg",
        "--collect-binaries",
        "numpy",
        str(ENTRYPOINT),
    ]
    run(command)


def main() -> int:
    try:
        ensure_runtime_dependencies()
        ensure_pyinstaller()
        build_exe()
        exe_path = PROJECT_ROOT / "dist" / "PluralEyesClone" / "PluralEyesClone.exe"
        print("")
        print("=" * 72)
        print("BUILD CONCLUIDO")
        print("=" * 72)
        print(f"Executavel: {exe_path}")
        print("Distribua a pasta inteira dist/PluralEyesClone, nao apenas o .exe.")
        print("=" * 72)
        return 0
    except subprocess.CalledProcessError as exc:
        print(f"[ERRO] Build falhou com exit={exc.returncode}", file=sys.stderr)
        return int(exc.returncode)
    except Exception as exc:
        print(f"[ERRO] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
