"""Validate a sync audit JSON against a known-good golden baseline.

Usage:
  python tools/validate_golden.py --write-golden
  python tools/validate_golden.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_AUDIT = (
    PROJECT_ROOT
    / "output"
    / "teste debora e lucas"
    / "casamento_soho_trackcheck_audit.json"
)
DEFAULT_GOLDEN = PROJECT_ROOT / "golden" / "casamento_soho_trackcheck_golden.json"
DEFAULT_OFFSET_TOLERANCE_SECONDS = 0.05
DEFAULT_DURATION_TOLERANCE_SECONDS = 0.05


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Valida um audit JSON contra um baseline golden de sincronizacao."
    )
    parser.add_argument(
        "--audit",
        default=str(DEFAULT_AUDIT),
        help=f"Audit JSON a validar. Padrao: {DEFAULT_AUDIT}",
    )
    parser.add_argument(
        "--golden",
        default=str(DEFAULT_GOLDEN),
        help=f"Golden baseline JSON. Padrao: {DEFAULT_GOLDEN}",
    )
    parser.add_argument(
        "--write-golden",
        action="store_true",
        help="Cria/substitui o golden a partir do audit informado.",
    )
    parser.add_argument(
        "--offset-tolerance",
        type=float,
        default=DEFAULT_OFFSET_TOLERANCE_SECONDS,
        help="Tolerancia maxima para offsets em segundos.",
    )
    parser.add_argument(
        "--duration-tolerance",
        type=float,
        default=DEFAULT_DURATION_TOLERANCE_SECONDS,
        help="Tolerancia maxima para duracoes em segundos.",
    )
    parser.add_argument(
        "--allow-cold-cache",
        action="store_true",
        help="Ignora divergencias de hit count do cache DSP.",
    )
    return parser.parse_args(argv)


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Arquivo nao encontrado: {path}")
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"JSON invalido: raiz precisa ser objeto em {path}")
    return payload


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def ok_rows(audit: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        row
        for row in audit.get("rows") or []
        if isinstance(row, dict) and row.get("status") == "ok"
    ]


def failed_rows(audit: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        row
        for row in audit.get("rows") or []
        if isinstance(row, dict) and row.get("status") != "ok"
    ]


def camera_track_order(audit: dict[str, Any]) -> dict[str, list[str]]:
    order: dict[str, list[str]] = {}
    track_check = audit.get("track_check") or {}
    for camera in track_check.get("cameras") or []:
        if not isinstance(camera, dict):
            continue
        camera_name = str(camera.get("camera_name") or "")
        entries = [
            str(entry.get("file_name"))
            for entry in camera.get("entries") or []
            if isinstance(entry, dict) and entry.get("file_name")
        ]
        order[camera_name] = entries
    return order


def build_golden_payload(audit: dict[str, Any], source_audit: Path) -> dict[str, Any]:
    metadata = audit.get("metadata") or {}
    track_check = audit.get("track_check") or {}
    successful = ok_rows(audit)
    failed = failed_rows(audit)
    clips = []
    baseline_name = source_audit.stem.removesuffix("_audit")

    for row in sorted(successful, key=lambda item: str(item.get("path") or "")):
        clips.append(
            {
                "path": row.get("path"),
                "file_name": row.get("file_name"),
                "camera_name": row.get("camera_name"),
                "offset_seconds": as_float(row.get("final_offset_seconds")),
                "duration_seconds": as_float(row.get("duration_seconds")),
                "timeline_end_seconds": as_float(row.get("timeline_end_seconds")),
            }
        )

    return {
        "schema_version": 1,
        "name": baseline_name,
        "source_audit": str(source_audit),
        "expected": {
            "reference_count": int(metadata.get("reference_count") or 0),
            "success_count": len(successful),
            "failure_count": len(failed),
            "track_overlap_count": int(track_check.get("total_overlap_count") or 0),
            "reference_cache_hit_features_count": int(
                metadata.get("reference_cache_hit_features_count") or 0
            ),
            "target_cache_hit_features_count": int(
                metadata.get("target_cache_hit_features_count") or 0
            ),
            "master_reference_name": metadata.get("master_reference_name"),
            "master_reference_track_name": metadata.get("master_reference_track_name"),
            "reference_match_scope": metadata.get("reference_match_scope"),
            "use_camera_clock_model": bool(metadata.get("use_camera_clock_model")),
            "spanning_continuity_adjustment_count": int(
                metadata.get("spanning_continuity_adjustment_count") or 0
            ),
        },
        "camera_track_order": camera_track_order(audit),
        "clips": clips,
    }


def collect_actual_summary(audit: dict[str, Any]) -> dict[str, Any]:
    metadata = audit.get("metadata") or {}
    track_check = audit.get("track_check") or {}
    return {
        "reference_count": int(metadata.get("reference_count") or 0),
        "success_count": len(ok_rows(audit)),
        "failure_count": len(failed_rows(audit)),
        "track_overlap_count": int(track_check.get("total_overlap_count") or 0),
        "reference_cache_hit_features_count": int(
            metadata.get("reference_cache_hit_features_count") or 0
        ),
        "target_cache_hit_features_count": int(
            metadata.get("target_cache_hit_features_count") or 0
        ),
        "master_reference_name": metadata.get("master_reference_name"),
        "master_reference_track_name": metadata.get("master_reference_track_name"),
        "reference_match_scope": metadata.get("reference_match_scope"),
        "use_camera_clock_model": bool(metadata.get("use_camera_clock_model")),
        "spanning_continuity_adjustment_count": int(
            metadata.get("spanning_continuity_adjustment_count") or 0
        ),
    }


def compare_scalar(
    failures: list[str],
    field: str,
    expected: Any,
    actual: Any,
) -> None:
    if expected != actual:
        failures.append(f"{field}: esperado {expected!r}, obtido {actual!r}")


def compare_float(
    failures: list[str],
    field: str,
    expected: float | None,
    actual: float | None,
    tolerance: float,
) -> None:
    if expected is None and actual is None:
        return
    if expected is None or actual is None:
        failures.append(f"{field}: esperado {expected!r}, obtido {actual!r}")
        return
    delta = abs(expected - actual)
    if delta > tolerance:
        failures.append(
            f"{field}: esperado {expected:.6f}, obtido {actual:.6f}, "
            f"delta {delta:.6f}s > tolerancia {tolerance:.6f}s"
        )


def validate_against_golden(
    audit: dict[str, Any],
    golden: dict[str, Any],
    *,
    offset_tolerance: float,
    duration_tolerance: float,
    allow_cold_cache: bool,
) -> list[str]:
    failures: list[str] = []
    expected_summary = golden.get("expected") or {}
    actual_summary = collect_actual_summary(audit)

    for field, expected in expected_summary.items():
        if allow_cold_cache and field in {
            "reference_cache_hit_features_count",
            "target_cache_hit_features_count",
        }:
            continue
        compare_scalar(failures, field, expected, actual_summary.get(field))

    expected_order = golden.get("camera_track_order") or {}
    actual_order = camera_track_order(audit)
    compare_scalar(failures, "camera_track_order", expected_order, actual_order)

    actual_by_path = {
        str(row.get("path")): row
        for row in ok_rows(audit)
        if row.get("path")
    }
    expected_by_path = {
        str(row.get("path")): row
        for row in golden.get("clips") or []
        if isinstance(row, dict) and row.get("path")
    }

    missing = sorted(set(expected_by_path) - set(actual_by_path))
    unexpected = sorted(set(actual_by_path) - set(expected_by_path))
    for path in missing:
        failures.append(f"clip ausente no audit atual: {Path(path).name} | {path}")
    for path in unexpected:
        failures.append(f"clip inesperado no audit atual: {Path(path).name} | {path}")

    for path in sorted(set(expected_by_path) & set(actual_by_path)):
        expected_row = expected_by_path[path]
        actual_row = actual_by_path[path]
        clip_name = str(expected_row.get("file_name") or Path(path).name)
        compare_scalar(
            failures,
            f"{clip_name}.camera_name",
            expected_row.get("camera_name"),
            actual_row.get("camera_name"),
        )
        compare_float(
            failures,
            f"{clip_name}.offset_seconds",
            as_float(expected_row.get("offset_seconds")),
            as_float(actual_row.get("final_offset_seconds")),
            offset_tolerance,
        )
        compare_float(
            failures,
            f"{clip_name}.duration_seconds",
            as_float(expected_row.get("duration_seconds")),
            as_float(actual_row.get("duration_seconds")),
            duration_tolerance,
        )

    return failures


def print_success(audit: dict[str, Any], golden_path: Path) -> None:
    metadata = audit.get("metadata") or {}
    track_check = audit.get("track_check") or {}
    camera_count = len((track_check.get("cameras") or []))
    print("[OK] Golden validado com sucesso")
    print(f"     Golden     : {golden_path}")
    print(f"     Sucesso    : {len(ok_rows(audit))} arquivo(s)")
    print(f"     Falhas     : {len(failed_rows(audit))} arquivo(s)")
    print(f"     TrackCheck : {track_check.get('total_overlap_count', 0)} overlap(s)")
    print(
        "     Cache DSP  : "
        f"refs {metadata.get('reference_cache_hit_features_count', 0)}/"
        f"{metadata.get('reference_count', 0)} | "
        f"targets {metadata.get('target_cache_hit_features_count', 0)}/"
        f"{len(ok_rows(audit))}"
    )
    print(f"     Cameras    : {camera_count}")


def print_failures(failures: list[str]) -> None:
    print("[FAIL] Golden divergiu do audit atual")
    for failure in failures:
        print(f"  - {failure}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    audit_path = Path(args.audit).expanduser().resolve()
    golden_path = Path(args.golden).expanduser().resolve()

    try:
        audit = load_json(audit_path)
        if args.write_golden:
            golden = build_golden_payload(audit, audit_path)
            write_json(golden_path, golden)
            print(f"[OK] Golden criado: {golden_path}")
            print(f"     Clips: {len(golden.get('clips') or [])}")
            return 0

        golden = load_json(golden_path)
        failures = validate_against_golden(
            audit,
            golden,
            offset_tolerance=args.offset_tolerance,
            duration_tolerance=args.duration_tolerance,
            allow_cold_cache=args.allow_cold_cache,
        )
        if failures:
            print_failures(failures)
            return 1
        print_success(audit, golden_path)
        return 0
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
