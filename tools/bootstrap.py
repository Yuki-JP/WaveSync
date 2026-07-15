"""Create a local venv, install runtime dependencies, and open the GUI.

This is intended for people running from source. The packaged .exe does not
need Python or pip on the user's machine.
"""

from __future__ import annotations

import os
import subprocess
import sys
import venv
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
VENV_DIR = PROJECT_ROOT / ".venv"
REQUIREMENTS = PROJECT_ROOT / "requirements.txt"
APP_SCRIPT = PROJECT_ROOT / "tkinter_app.py"


def venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def run(command: list[str], *, cwd: Path = PROJECT_ROOT) -> None:
    print(" ".join(f'"{item}"' if " " in item else item for item in command), flush=True)
    subprocess.run(command, cwd=cwd, check=True)


def ensure_supported_python() -> None:
    if sys.version_info < (3, 9):
        raise RuntimeError("Python 3.9 ou superior e necessario.")


def ensure_venv() -> Path:
    python_path = venv_python()
    if python_path.exists():
        return python_path

    print(f"[SETUP] Criando ambiente local: {VENV_DIR}", flush=True)
    venv.EnvBuilder(with_pip=True, clear=False).create(VENV_DIR)
    return python_path


def install_requirements(python_path: Path) -> None:
    if not REQUIREMENTS.exists():
        raise FileNotFoundError(f"requirements.txt nao encontrado: {REQUIREMENTS}")

    print("[SETUP] Instalando/atualizando dependencias...", flush=True)
    run([str(python_path), "-m", "pip", "install", "--upgrade", "pip"])
    run([str(python_path), "-m", "pip", "install", "-r", str(REQUIREMENTS)])


def verify_runtime(python_path: Path) -> None:
    print("[SETUP] Verificando dependencias...", flush=True)
    run(
        [
            str(python_path),
            "-c",
            "import numpy, imageio_ffmpeg; print('Dependencias OK')",
        ]
    )


def open_app(python_path: Path) -> int:
    print("[SETUP] Abrindo interface...", flush=True)
    completed = subprocess.run([str(python_path), str(APP_SCRIPT)], cwd=PROJECT_ROOT)
    return int(completed.returncode)


def main() -> int:
    try:
        ensure_supported_python()
        python_path = ensure_venv()
        install_requirements(python_path)
        verify_runtime(python_path)
        return open_app(python_path)
    except subprocess.CalledProcessError as exc:
        print(f"[ERRO] Comando falhou com exit={exc.returncode}", file=sys.stderr)
        return int(exc.returncode)
    except Exception as exc:
        print(f"[ERRO] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
