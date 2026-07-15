"""Run known-good sync regressions from config to golden validation.

Default flow runs every official baseline:
  1. python main.py --config <baseline config>
  2. python tools/validate_golden.py --audit <baseline audit> --golden <baseline golden>
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
VALIDATOR_SCRIPT = PROJECT_ROOT / "tools" / "validate_golden.py"
MAIN_SCRIPT = PROJECT_ROOT / "main.py"


@dataclass(frozen=True)
class RegressionCase:
    name: str
    config: Path
    audit: Path
    golden: Path


REGRESSION_CASES = {
    "soho": RegressionCase(
        name="soho",
        config=PROJECT_ROOT / "configs" / "casamento_soho_trackcheck.json",
        audit=(
            PROJECT_ROOT
            / "output"
            / "teste debora e lucas"
            / "casamento_soho_trackcheck_audit.json"
        ),
        golden=PROJECT_ROOT / "golden" / "casamento_soho_trackcheck_golden.json",
    ),
    "juliana-caue": RegressionCase(
        name="juliana-caue",
        config=PROJECT_ROOT / "configs" / "casamento_juliana_caue_teste_limpo.json",
        audit=(
            PROJECT_ROOT
            / "output"
            / "teste juliana e caue"
            / "casamento_juliana_caue_teste_limpo_audit.json"
        ),
        golden=PROJECT_ROOT
        / "golden"
        / "casamento_juliana_caue_teste_limpo_golden.json",
    ),
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Executa testes de regressao do sync multicamera funcional."
    )
    parser.add_argument(
        "--case",
        action="append",
        choices=sorted(REGRESSION_CASES),
        help="Baseline oficial a executar. Pode ser repetido. Padrao: todos.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Preset JSON manual. Quando usado, executa apenas este config.",
    )
    parser.add_argument(
        "--audit",
        default=None,
        help="Audit JSON esperado para validacao do config manual.",
    )
    parser.add_argument(
        "--golden",
        default=None,
        help="Golden JSON esperado para validacao do config manual.",
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


def selected_cases(args: argparse.Namespace) -> list[RegressionCase]:
    if args.config:
        return [
            RegressionCase(
                name="manual",
                config=Path(args.config).expanduser().resolve(),
                audit=(
                    Path(args.audit).expanduser().resolve()
                    if args.audit
                    else REGRESSION_CASES["soho"].audit
                ),
                golden=(
                    Path(args.golden).expanduser().resolve()
                    if args.golden
                    else REGRESSION_CASES["soho"].golden
                ),
            )
        ]

    names = args.case or list(REGRESSION_CASES)
    return [REGRESSION_CASES[name] for name in names]


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


def build_validation_command(
    args: argparse.Namespace,
    audit_path: Path,
    golden_path: Path,
) -> list[str]:
    command = [
        args.python,
        str(VALIDATOR_SCRIPT),
        "--audit",
        str(audit_path),
        "--golden",
        str(golden_path),
    ]
    if args.allow_cold_cache:
        command.append("--allow-cold-cache")
    if args.offset_tolerance is not None:
        command.extend(["--offset-tolerance", str(args.offset_tolerance)])
    if args.duration_tolerance is not None:
        command.extend(["--duration-tolerance", str(args.duration_tolerance)])
    return command


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    python_path = Path(args.python).expanduser()
    cases = selected_cases(args)

    try:
        ensure_file(MAIN_SCRIPT, "main.py")
        ensure_file(VALIDATOR_SCRIPT, "validate_golden.py")
        if not python_path.exists():
            raise FileNotFoundError(f"Python nao encontrado: {python_path}")
        for case in cases:
            ensure_file(case.config, f"Config de regressao ({case.name})")
            if args.validate_only:
                ensure_file(case.audit, f"Audit de regressao ({case.name})")
            ensure_file(case.golden, f"Golden de regressao ({case.name})")

        emit("[REGRESSION] PluralEyes clone - baseline multicamera funcional")
        emit(f"[REGRESSION] Projeto : {PROJECT_ROOT}")
        emit("[REGRESSION] Casos   : " + ", ".join(case.name for case in cases))

        overall_start = time.perf_counter()

        if args.validate_only:
            emit("[REGRESSION] Modo validate-only: sync nao sera executado.")

        for index, case in enumerate(cases, start=1):
            emit("")
            emit("-" * 72)
            emit(f"[REGRESSION] Caso {index}/{len(cases)}: {case.name}")
            emit(f"[REGRESSION] Config : {case.config}")
            emit(f"[REGRESSION] Audit  : {case.audit}")
            emit(f"[REGRESSION] Golden : {case.golden}")

            if not args.validate_only:
                sync_code = run_step(
                    f"{case.name} - Gerar XML/audit pelo preset golden",
                    build_sync_command(args, case.config),
                    args.timeout,
                )
                if sync_code != 0:
                    return sync_code

            validation_code = run_step(
                f"{case.name} - Validar audit contra golden",
                build_validation_command(args, case.audit, case.golden),
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
