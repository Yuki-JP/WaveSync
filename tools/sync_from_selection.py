"""Generate a config from a selection JSON and run the sync pipeline.

Example:
  python tools/sync_from_selection.py --selection selections/casamento_soho_selection.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from make_config import (
    apply_selection,
    build_config,
    parse_args as parse_make_config_args,
    print_summary,
    write_config,
)


def application_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


PROJECT_ROOT = application_root()
MAIN_SCRIPT = PROJECT_ROOT / "main.py"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Gera config a partir de selection JSON e roda main.py --config."
    )
    parser.add_argument(
        "--selection",
        "--from-selection",
        dest="selection",
        required=True,
        help="Arquivo JSON de selecao usado para gerar o config.",
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Executavel Python para chamar main.py. Padrao: o Python atual.",
    )
    parser.add_argument(
        "--config-output",
        default=None,
        help="Sobrescreve o caminho do config gerado.",
    )
    parser.add_argument(
        "--xml-output",
        default=None,
        help="Sobrescreve o caminho do XML gerado pelo sync.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Mostra o config que seria gerado, mas nao grava nem roda sync.",
    )
    parser.add_argument(
        "--config-only",
        action="store_true",
        help="Grava/atualiza o config, mas nao roda sync.",
    )
    parser.add_argument(
        "--no-overwrite-config",
        action="store_true",
        help="Falha se o config gerado ja existir.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Timeout do main.py em segundos. Padrao: sem timeout.",
    )
    return parser.parse_args(argv)


def emit(message: str = "") -> None:
    print(message, flush=True)


def quote_arg(value: str) -> str:
    if not value or any(char.isspace() for char in value):
        return f'"{value}"'
    return value


def format_command(command: list[str]) -> str:
    return " ".join(quote_arg(item) for item in command)


def ensure_file(path: Path, label: str) -> None:
    if not path.exists():
        raise FileNotFoundError(f"{label} nao encontrado: {path}")
    if not path.is_file():
        raise ValueError(f"{label} deve ser arquivo: {path}")


def build_make_config_argv(args: argparse.Namespace) -> list[str]:
    argv = ["--from-selection", str(Path(args.selection).expanduser())]
    if args.config_output:
        argv.extend(["--config-output", args.config_output])
    if args.xml_output:
        argv.extend(["--xml-output", args.xml_output])
    return argv


def run_sync(python_executable: str, config_path: Path, timeout: float | None) -> int:
    command = [
        python_executable,
        str(MAIN_SCRIPT),
        "--config",
        str(config_path),
    ]
    emit("")
    emit("=" * 72)
    emit("SYNC")
    emit("=" * 72)
    emit(format_command(command))
    emit("")

    start = time.perf_counter()
    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )
    elapsed = time.perf_counter() - start
    emit("")
    if completed.returncode == 0:
        emit(f"[OK] Sync concluido em {elapsed:.1f}s")
    else:
        emit(f"[FAIL] Sync falhou em {elapsed:.1f}s | exit={completed.returncode}")
    return int(completed.returncode)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        ensure_file(MAIN_SCRIPT, "main.py")
        make_args = apply_selection(parse_make_config_args(build_make_config_argv(args)))
        config, config_path = build_config(make_args)

        emit("[SYNC-FROM-SELECTION] PluralEyes clone")
        emit(f"[SYNC-FROM-SELECTION] Selection : {Path(args.selection).expanduser()}")
        emit(f"[SYNC-FROM-SELECTION] Config    : {config_path}")
        print_summary(config, config_path)

        if args.dry_run:
            emit("")
            emit(json.dumps(config, ensure_ascii=False, indent=2))
            return 0

        write_config(
            config,
            config_path,
            overwrite=not args.no_overwrite_config,
        )
        emit("")
        emit(f"[OK] Config salvo: {config_path}")

        if args.config_only:
            emit("[OK] Modo config-only: sync nao sera executado.")
            return 0

        return run_sync(args.python, config_path, args.timeout)
    except subprocess.TimeoutExpired as exc:
        emit(f"[ERROR] Timeout apos {exc.timeout}s: {format_command(exc.cmd)}")
        return 124
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
