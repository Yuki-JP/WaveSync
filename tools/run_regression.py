"""Run the known-good sync regression from config to golden validation.

Default flow:
  1. python main.py --config configs/casamento_soho_trackcheck.json
  2. python tools/validate_golden.py
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / "configs" / "casamento_soho_trackcheck.json"
VALIDATOR_SCRIPT = PROJECT_ROOT / "tools" / "validate_golden.py"
MAIN_SCRIPT = PROJECT_ROOT / "main.py"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Executa o teste de regressao do sync multicamera funcional."
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        help=f"Preset JSON usado no teste. Padrao: {DEFAULT_CONFIG}",
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Executavel Python a usar nos subprocessos. Padrao: o Python atual.",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Nao roda o sync; valida apenas o audit JSON ja existente.",
    )
    parser.add_argument(
        "--allow-cold-cache",
        action="store_true",
        help="Nao exige hit count do cache DSP na validacao golden.",
    )
    parser.add_argument(
        "--offset-tolerance",
        type=float,
        default=None,
        help="Tolerancia de offset repassada ao validate_golden.py.",
    )
    parser.add_argument(
        "--duration-tolerance",
        type=float,
        default=None,
        help="Tolerancia de duracao repassada ao validate_golden.py.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Timeout por etapa em segundos. Padrao: sem timeout.",
    )
    return parser.parse_args(argv)


def ensure_file(path: Path, label: str) -> None:
    if not path.exists():
        raise FileNotFoundError(f"{label} nao encontrado: {path}")
    if not path.is_file():
        raise ValueError(f"{label} deve ser arquivo: {path}")


def format_command(command: list[str]) -> str:
    return " ".join(quote_arg(item) for item in command)


def quote_arg(value: str) -> str:
    if not value or any(char.isspace() for char in value):
        return f'"{value}"'
    return value


def emit(message: str = "") -> None:
    print(message, flush=True)


def run_step(label: str, command: list[str], timeout: float | None) -> int:
    emit("")
    emit("=" * 72)
    emit(label)
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
        emit(f"[OK] {label} concluido em {elapsed:.1f}s")
    else:
        emit(f"[FAIL] {label} falhou em {elapsed:.1f}s | exit={completed.returncode}")
    return int(completed.returncode)


def build_sync_command(args: argparse.Namespace, config_path: Path) -> list[str]:
    return [
        args.python,
        str(MAIN_SCRIPT),
        "--config",
        str(config_path),
    ]


def build_validation_command(args: argparse.Namespace) -> list[str]:
    command = [args.python, str(VALIDATOR_SCRIPT)]
    if args.allow_cold_cache:
        command.append("--allow-cold-cache")
    if args.offset_tolerance is not None:
        command.extend(["--offset-tolerance", str(args.offset_tolerance)])
    if args.duration_tolerance is not None:
        command.extend(["--duration-tolerance", str(args.duration_tolerance)])
    return command


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_path = Path(args.config).expanduser().resolve()
    python_path = Path(args.python).expanduser()

    try:
        ensure_file(MAIN_SCRIPT, "main.py")
        ensure_file(VALIDATOR_SCRIPT, "validate_golden.py")
        ensure_file(config_path, "Config de regressao")
        if not python_path.exists():
            raise FileNotFoundError(f"Python nao encontrado: {python_path}")

        emit("[REGRESSION] PluralEyes clone - baseline multicamera funcional")
        emit(f"[REGRESSION] Projeto : {PROJECT_ROOT}")
        emit(f"[REGRESSION] Config  : {config_path}")

        overall_start = time.perf_counter()

        if not args.validate_only:
            sync_code = run_step(
                "Etapa 1/2 - Gerar XML/audit pelo preset golden",
                build_sync_command(args, config_path),
                args.timeout,
            )
            if sync_code != 0:
                return sync_code
        else:
            emit("[REGRESSION] Modo validate-only: sync nao sera executado.")

        validation_code = run_step(
            "Etapa 2/2 - Validar audit contra golden",
            build_validation_command(args),
            args.timeout,
        )
        if validation_code != 0:
            return validation_code

        elapsed = time.perf_counter() - overall_start
        emit("")
        emit("=" * 72)
        emit(f"[OK] REGRESSAO APROVADA em {elapsed:.1f}s")
        emit("=" * 72)
        return 0
    except subprocess.TimeoutExpired as exc:
        emit(f"[ERROR] Timeout apos {exc.timeout}s: {format_command(exc.cmd)}")
        return 124
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
