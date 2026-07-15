"""Validate a media selection before running the sync pipeline.

Usage:
  python tools/validate_selection.py --selection selections/casamento.json
  python tools/validate_selection.py --selection selections/casamento.json --no-probe-duration
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from make_config import (
    AUDIO_EXTENSIONS,
    DEFAULT_CONFIG_DIR,
    PROJECT_ROOT,
    VIDEO_EXTENSIONS,
    apply_selection,
    build_config,
    parse_args as parse_make_config_args,
    slugify,
)


@dataclass(frozen=True)
class MediaEntry:
    group_name: str
    path: Path
    kind: str
    duration_seconds: float | None
    size_bytes: int


@dataclass
class ValidationReport:
    errors: list[str]
    warnings: list[str]
    references: list[MediaEntry]
    targets: list[MediaEntry]
    config_path: Path
    xml_output: str
    explicit_selection: bool
    ignore_metadata: bool
    use_camera_clock_model: bool


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Valida uma selection JSON antes de gerar/sincronizar."
    )
    parser.add_argument(
        "--selection",
        "--from-selection",
        required=True,
        help="Arquivo JSON de selection.",
    )
    parser.add_argument(
        "--config-output",
        default=None,
        help="Sobrescreve o caminho do config previsto.",
    )
    parser.add_argument(
        "--xml-output",
        default=None,
        help="Sobrescreve o caminho do XML previsto.",
    )
    parser.add_argument(
        "--no-probe-duration",
        action="store_true",
        help="Nao usa ffprobe para estimar duracoes.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Imprime o relatorio em JSON.",
    )
    return parser.parse_args(argv)


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"Selection deve conter um objeto JSON: {path}")
    return payload


def build_make_args(args: argparse.Namespace, selection_path: Path) -> argparse.Namespace:
    argv = ["--from-selection", str(selection_path), "--dry-run"]
    if args.config_output:
        argv.extend(["--config-output", args.config_output])
    if args.xml_output:
        argv.extend(["--xml-output", args.xml_output])
    return apply_selection(parse_make_config_args(argv))


def ffprobe_duration(path: Path, *, enabled: bool) -> float | None:
    if not enabled:
        return None

    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None

    command = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:
        return None
    if completed.returncode != 0:
        return None

    text = completed.stdout.strip()
    if not text or text.upper() == "N/A":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def resolve_media_entries(
    grouped: dict[str, Any],
    *,
    extensions: set[str],
    kind: str,
    probe_duration: bool,
    errors: list[str],
    warnings: list[str],
) -> list[MediaEntry]:
    entries: list[MediaEntry] = []
    seen_in_group: set[tuple[str, str]] = set()

    for group_name, raw_paths in grouped.items():
        if not isinstance(raw_paths, list):
            errors.append(f"{kind}.{group_name}: grupo deve ser lista de arquivos.")
            continue
        if not raw_paths:
            errors.append(f"{kind}.{group_name}: grupo vazio.")
            continue

        for raw_path in raw_paths:
            path = Path(str(raw_path)).expanduser()
            path_key = str(path).casefold()
            group_key = (str(group_name).casefold(), path_key)
            if group_key in seen_in_group:
                warnings.append(f"{kind}.{group_name}: arquivo repetido no mesmo grupo: {path}")
                continue
            seen_in_group.add(group_key)

            if not path.exists():
                errors.append(f"{kind}.{group_name}: arquivo nao existe: {path}")
                continue
            if not path.is_file():
                errors.append(f"{kind}.{group_name}: caminho nao e arquivo: {path}")
                continue
            if path.suffix.casefold() not in extensions:
                supported = ", ".join(sorted(extensions))
                errors.append(
                    f"{kind}.{group_name}: extensao invalida: {path.name} "
                    f"(suportadas: {supported})"
                )
                continue

            lowered = str(path).casefold()
            if kind == "targets" and "proxy" in lowered:
                errors.append(f"{kind}.{group_name}: proxy selecionado: {path}")
                continue
            if kind == "references" and (
                "drift_corrected" in lowered or "pluraleyes_drift_corrected" in lowered
            ):
                errors.append(f"{kind}.{group_name}: drift_corrected selecionado: {path}")
                continue

            try:
                stat = path.stat()
            except OSError as exc:
                errors.append(f"{kind}.{group_name}: nao foi possivel ler {path}: {exc}")
                continue

            entries.append(
                MediaEntry(
                    group_name=str(group_name),
                    path=path.resolve(),
                    kind=kind,
                    duration_seconds=ffprobe_duration(path, enabled=probe_duration),
                    size_bytes=int(stat.st_size),
                )
            )

    return entries


def detect_cross_group_duplicates(
    entries: list[MediaEntry],
    *,
    label: str,
    errors: list[str],
) -> None:
    seen: dict[str, str] = {}
    for entry in entries:
        key = str(entry.path).casefold()
        if key in seen and seen[key] != entry.group_name:
            errors.append(
                f"{label}: arquivo em mais de um grupo: {entry.path.name} "
                f"({seen[key]} e {entry.group_name})"
            )
            continue
        seen[key] = entry.group_name


def detect_slug_collisions(
    group_names: list[str],
    *,
    label: str,
    warnings: list[str],
) -> None:
    slugs: dict[str, str] = {}
    for group_name in group_names:
        slug = slugify(group_name)
        if slug in slugs and slugs[slug] != group_name:
            warnings.append(
                f"{label}: grupos viram o mesmo slug '{slug}': "
                f"{slugs[slug]!r} e {group_name!r}"
            )
        slugs[slug] = group_name


def output_inside_project_output(xml_output: str) -> bool:
    path = Path(xml_output).expanduser()
    if not path.is_absolute():
        path = (PROJECT_ROOT / path).resolve()
    else:
        path = path.resolve()
    try:
        path.relative_to((PROJECT_ROOT / "output").resolve())
        return True
    except ValueError:
        return False


def validate_selection(
    args: argparse.Namespace,
    selection_path: Path,
    *,
    probe_duration: bool,
) -> ValidationReport:
    payload = load_json(selection_path)
    make_args = build_make_args(args, selection_path)
    config, config_path = build_config(make_args)

    errors: list[str] = []
    warnings: list[str] = []

    references = config.get("references")
    targets = config.get("targets")
    if not isinstance(references, dict):
        errors.append("references/reference_groups nao gerou grupos validos.")
        references = {}
    if not isinstance(targets, dict):
        errors.append("targets/target_groups nao gerou grupos validos.")
        targets = {}

    reference_entries = resolve_media_entries(
        references,
        extensions=AUDIO_EXTENSIONS,
        kind="references",
        probe_duration=probe_duration,
        errors=errors,
        warnings=warnings,
    )
    target_entries = resolve_media_entries(
        targets,
        extensions=VIDEO_EXTENSIONS,
        kind="targets",
        probe_duration=probe_duration,
        errors=errors,
        warnings=warnings,
    )

    detect_cross_group_duplicates(reference_entries, label="references", errors=errors)
    detect_cross_group_duplicates(target_entries, label="targets", errors=errors)
    detect_slug_collisions(list(references), label="references", warnings=warnings)
    detect_slug_collisions(list(targets), label="targets", warnings=warnings)

    explicit_selection = bool(
        config.get("explicit_selection") or payload.get("explicit_selection")
    )
    ignore_metadata = bool(config.get("ignore_metadata"))
    use_camera_clock_model = bool(config.get("use_camera_clock_model"))
    xml_output = str(config.get("output") or "")

    if not explicit_selection:
        warnings.append(
            "selection nao esta marcada como explicit_selection=true; "
            "modo por filtro/range pode incluir arquivos inesperados."
        )
    if not ignore_metadata:
        warnings.append("ignore_metadata=false; relogios ruins podem afetar o sync.")
    if not use_camera_clock_model:
        warnings.append("use_camera_clock_model=false; cortes longos podem perder precisao.")
    if not xml_output:
        errors.append("output XML nao definido.")
    elif not output_inside_project_output(xml_output):
        warnings.append(f"output XML fora da pasta output/: {xml_output}")

    if config_path.exists():
        warnings.append(f"config sera sobrescrito/ja existe: {config_path}")
    elif config_path.parent != DEFAULT_CONFIG_DIR:
        warnings.append(f"config sera salvo fora de configs/: {config_path}")

    if not reference_entries:
        errors.append("nenhuma referencia de audio valida.")
    if not target_entries:
        errors.append("nenhum video target valido.")

    return ValidationReport(
        errors=errors,
        warnings=warnings,
        references=reference_entries,
        targets=target_entries,
        config_path=config_path,
        xml_output=xml_output,
        explicit_selection=explicit_selection,
        ignore_metadata=ignore_metadata,
        use_camera_clock_model=use_camera_clock_model,
    )


def duration_text(seconds: float | None) -> str:
    if seconds is None:
        return "n/d"
    minutes, sec = divmod(float(seconds), 60.0)
    hours, minutes = divmod(int(minutes), 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{sec:05.2f}"
    return f"{minutes:02d}:{sec:05.2f}"


def bytes_to_gib(size_bytes: int) -> float:
    return size_bytes / (1024.0**3)


def grouped_summary(entries: list[MediaEntry]) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    for entry in entries:
        bucket = summary.setdefault(
            entry.group_name,
            {
                "count": 0,
                "size_bytes": 0,
                "duration_seconds": 0.0,
                "duration_unknown": 0,
                "files": [],
            },
        )
        bucket["count"] += 1
        bucket["size_bytes"] += entry.size_bytes
        if entry.duration_seconds is None:
            bucket["duration_unknown"] += 1
        else:
            bucket["duration_seconds"] += entry.duration_seconds
        bucket["files"].append(entry.path.name)
    return summary


def print_grouped(label: str, entries: list[MediaEntry]) -> None:
    summary = grouped_summary(entries)
    count = sum(int(item["count"]) for item in summary.values())
    print(f"{label:<15}: {count} arquivo(s) em {len(summary)} grupo(s)")
    for group_name, item in summary.items():
        duration = (
            "n/d"
            if int(item["duration_unknown"]) == int(item["count"])
            else duration_text(float(item["duration_seconds"]))
        )
        unknown = int(item["duration_unknown"])
        suffix = f" | duracao n/d: {unknown}" if unknown else ""
        print(
            f"  - {group_name}: {item['count']} arquivo(s) | "
            f"{bytes_to_gib(int(item['size_bytes'])):.2f} GiB | duracao {duration}{suffix}"
        )


def report_to_json(report: ValidationReport, selection_path: Path) -> dict[str, Any]:
    def entry_payload(entry: MediaEntry) -> dict[str, Any]:
        return {
            "group_name": entry.group_name,
            "file_name": entry.path.name,
            "path": str(entry.path),
            "kind": entry.kind,
            "duration_seconds": entry.duration_seconds,
            "size_bytes": entry.size_bytes,
        }

    return {
        "selection": str(selection_path),
        "config_path": str(report.config_path),
        "xml_output": report.xml_output,
        "explicit_selection": report.explicit_selection,
        "ignore_metadata": report.ignore_metadata,
        "use_camera_clock_model": report.use_camera_clock_model,
        "errors": report.errors,
        "warnings": report.warnings,
        "references": [entry_payload(entry) for entry in report.references],
        "targets": [entry_payload(entry) for entry in report.targets],
    }


def print_report(report: ValidationReport, selection_path: Path) -> None:
    print("")
    print("=" * 72)
    print("VALIDACAO DA SELECTION")
    print("=" * 72)
    print(f"Selection       : {selection_path}")
    print(f"Config previsto : {report.config_path}")
    print(f"Output XML      : {report.xml_output}")
    print(f"Selecao explic. : {report.explicit_selection}")
    print(f"Ignore metadata : {report.ignore_metadata}")
    print(f"Clock model     : {report.use_camera_clock_model}")
    print("-" * 72)
    print_grouped("Referencias", report.references)
    print_grouped("Targets", report.targets)

    if report.warnings:
        print("-" * 72)
        print("WARNINGS")
        for warning in report.warnings:
            print(f"  [WARN] {warning}")

    if report.errors:
        print("-" * 72)
        print("ERROS")
        for error in report.errors:
            print(f"  [ERRO] {error}")

    print("=" * 72)
    if report.errors:
        print("[FAIL] Selection invalida.")
    else:
        print("[OK] Selection valida para gerar config/sync.")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    selection_path = Path(args.selection).expanduser().resolve()

    try:
        report = validate_selection(
            args,
            selection_path,
            probe_duration=not args.no_probe_duration,
        )
        if args.json:
            print(json.dumps(report_to_json(report, selection_path), ensure_ascii=False, indent=2))
        else:
            print_report(report, selection_path)
        return 1 if report.errors else 0
    except Exception as exc:
        if args.json:
            print(
                json.dumps(
                    {
                        "selection": str(selection_path),
                        "errors": [str(exc)],
                        "warnings": [],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
