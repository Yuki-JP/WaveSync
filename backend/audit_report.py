"""Audit, summary, and track validation helpers for the sync pipeline."""

from __future__ import annotations

import csv
import json
import logging
from datetime import datetime
from pathlib import Path


TRACK_CHECK_OVERLAP_TOLERANCE_SECONDS = 1.0 / 30.0

logger = logging.getLogger("wavesync.pipeline")


AUDIT_COLUMNS = [
    "status",
    "file_name",
    "path",
    "camera_name",
    "duration_seconds",
    "final_offset_seconds",
    "final_offset_timecode",
    "timeline_end_seconds",
    "extracted_wav_path",
    "cache_hit_wav",
    "cache_hit_features",
    "previous_clip",
    "timeline_gap_from_previous_seconds",
    "chosen_reference_name",
    "chosen_reference_path",
    "reference_to_master_delta_seconds",
    "sync_method",
    "offset_decision_reason",
    "correlation_source",
    "correlation_z_score",
    "correlation_prominence_ratio",
    "correlation_low_confidence",
    "raw_correlation_offset_seconds",
    "individual_dsp_offset_seconds",
    "individual_vs_final_delta_seconds",
    "camera_native_predicted_offset_seconds",
    "final_vs_native_prediction_delta_seconds",
    "camera_clock_model_offset_seconds",
    "final_vs_camera_clock_model_delta_seconds",
    "camera_clock_residual_seconds",
    "camera_clock_base_seconds",
    "camera_clock_drift_rate",
    "camera_clock_drift_ppm",
    "camera_clock_inlier_count",
    "camera_clock_candidate_count",
    "camera_clock_model_method",
    "camera_local_refine_reference_name",
    "camera_local_refine_offset_seconds",
    "camera_local_refine_delta_seconds",
    "camera_local_refine_z_score",
    "camera_local_refine_prominence_ratio",
    "camera_peer_refine_reference_clip",
    "camera_peer_refine_reference_camera",
    "camera_peer_refine_offset_seconds",
    "camera_peer_refine_delta_seconds",
    "camera_peer_refine_z_score",
    "camera_peer_refine_prominence_ratio",
    "camera_peer_refine_overlap_seconds",
    "camera_native_relative_start_seconds",
    "camera_native_gap_from_previous_seconds",
    "camera_block_base_seconds",
    "camera_base_candidate_seconds",
    "camera_base_deviation_seconds",
    "camera_block_anchor_name",
    "camera_block_anchor_z_score",
    "spanning_continuity_applied",
    "spanning_previous_path",
    "spanning_gap_seconds",
    "spanning_old_offset_seconds",
    "spanning_new_offset_seconds",
    "skip_reason",
    "error",
]


def write_sync_audit_reports(
    sync_results: dict,
    output_xml_path: Path,
    audit_output: str | None = None,
) -> tuple[Path, Path]:
    csv_path, json_path = resolve_audit_output_paths(output_xml_path, audit_output)
    rows = build_sync_audit_rows(sync_results)
    camera_summary = build_camera_audit_summary(rows)
    track_check = build_camera_track_check(rows)

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8-sig") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=AUDIT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    metadata = sync_results.setdefault("metadata", {})
    metadata["audit_reports"] = {
        "csv": str(csv_path),
        "json": str(json_path),
    }
    metadata["track_check"] = track_check
    json_payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "metadata": metadata,
        "references": sync_results.get("references") or [],
        "camera_summary": camera_summary,
        "track_check": track_check,
        "rows": rows,
    }
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(json_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return csv_path, json_path


def resolve_audit_output_paths(
    output_xml_path: Path,
    audit_output: str | None,
) -> tuple[Path, Path]:
    if audit_output:
        requested = Path(audit_output).expanduser().resolve()
        if requested.suffix.casefold() == ".csv":
            return requested, requested.with_suffix(".json")
        if requested.suffix.casefold() == ".json":
            return requested.with_suffix(".csv"), requested
        return requested.with_suffix(".csv"), requested.with_suffix(".json")

    audit_base = output_xml_path.with_name(f"{output_xml_path.stem}_audit")
    return audit_base.with_suffix(".csv"), audit_base.with_suffix(".json")


def build_sync_audit_rows(sync_results: dict) -> list[dict]:
    offsets = sync_results.get("offsets") or {}
    metadata = sync_results.get("metadata") or {}
    rows: list[dict] = []

    for path_text, raw_offset in offsets.items():
        item_metadata = metadata.get(path_text) or {}
        final_offset = sync_offset_seconds(raw_offset)
        duration_seconds = first_number(
            item_metadata.get("repaired_duration_seconds"),
            item_metadata.get("duration_seconds"),
        )
        status = "ok" if final_offset is not None else "failed"
        timeline_end = (
            None
            if final_offset is None or duration_seconds is None
            else final_offset + duration_seconds
        )
        individual_offset = first_number(item_metadata.get("individual_dsp_offset_seconds"))
        native_prediction = first_number(
            item_metadata.get("camera_native_predicted_offset_seconds")
        )
        clock_model_offset = first_number(
            item_metadata.get("camera_clock_model_offset_seconds")
        )

        rows.append(
            {
                "status": status,
                "file_name": Path(path_text).name,
                "path": path_text,
                "camera_name": first_text(
                    item_metadata.get("camera_name"),
                    item_metadata.get("camera"),
                    item_metadata.get("device"),
                    Path(path_text).parent.name,
                ),
                "duration_seconds": duration_seconds,
                "final_offset_seconds": final_offset,
                "final_offset_timecode": (
                    None if final_offset is None else format_offset(final_offset)
                ),
                "timeline_end_seconds": timeline_end,
                "extracted_wav_path": item_metadata.get("extracted_wav_path"),
                "cache_hit_wav": bool(item_metadata.get("cache_hit_wav")),
                "cache_hit_features": bool(item_metadata.get("cache_hit_features")),
                "chosen_reference_name": item_metadata.get("chosen_reference_name"),
                "chosen_reference_path": item_metadata.get("chosen_reference_path"),
                "reference_to_master_delta_seconds": first_number(
                    item_metadata.get("reference_to_master_delta_seconds")
                ),
                "sync_method": item_metadata.get("sync_method"),
                "offset_decision_reason": item_metadata.get("offset_decision_reason"),
                "correlation_source": item_metadata.get("correlation_source"),
                "correlation_z_score": first_number(
                    item_metadata.get("correlation_z_score")
                ),
                "correlation_prominence_ratio": first_number(
                    item_metadata.get("correlation_prominence_ratio")
                ),
                "correlation_low_confidence": item_metadata.get("correlation_low_confidence"),
                "raw_correlation_offset_seconds": first_number(
                    item_metadata.get("raw_correlation_offset_seconds")
                ),
                "individual_dsp_offset_seconds": individual_offset,
                "individual_vs_final_delta_seconds": delta(final_offset, individual_offset),
                "camera_native_predicted_offset_seconds": native_prediction,
                "final_vs_native_prediction_delta_seconds": delta(
                    final_offset,
                    native_prediction,
                ),
                "camera_clock_model_offset_seconds": clock_model_offset,
                "final_vs_camera_clock_model_delta_seconds": delta(
                    final_offset,
                    clock_model_offset,
                ),
                "camera_clock_residual_seconds": first_number(
                    item_metadata.get("camera_clock_residual_seconds")
                ),
                "camera_clock_base_seconds": first_number(
                    item_metadata.get("camera_clock_base_seconds")
                ),
                "camera_clock_drift_rate": first_number(
                    item_metadata.get("camera_clock_drift_rate")
                ),
                "camera_clock_drift_ppm": first_number(
                    item_metadata.get("camera_clock_drift_ppm")
                ),
                "camera_clock_inlier_count": first_number(
                    item_metadata.get("camera_clock_inlier_count")
                ),
                "camera_clock_candidate_count": first_number(
                    item_metadata.get("camera_clock_candidate_count")
                ),
                "camera_clock_model_method": item_metadata.get("camera_clock_model_method"),
                "camera_local_refine_reference_name": item_metadata.get(
                    "camera_local_refine_reference_name"
                ),
                "camera_local_refine_offset_seconds": first_number(
                    item_metadata.get("camera_local_refine_offset_seconds")
                ),
                "camera_local_refine_delta_seconds": first_number(
                    item_metadata.get("camera_local_refine_delta_seconds")
                ),
                "camera_local_refine_z_score": first_number(
                    item_metadata.get("camera_local_refine_z_score")
                ),
                "camera_local_refine_prominence_ratio": first_number(
                    item_metadata.get("camera_local_refine_prominence_ratio")
                ),
                "camera_peer_refine_reference_clip": item_metadata.get(
                    "camera_peer_refine_reference_clip"
                ),
                "camera_peer_refine_reference_camera": item_metadata.get(
                    "camera_peer_refine_reference_camera"
                ),
                "camera_peer_refine_offset_seconds": first_number(
                    item_metadata.get("camera_peer_refine_offset_seconds")
                ),
                "camera_peer_refine_delta_seconds": first_number(
                    item_metadata.get("camera_peer_refine_delta_seconds")
                ),
                "camera_peer_refine_z_score": first_number(
                    item_metadata.get("camera_peer_refine_z_score")
                ),
                "camera_peer_refine_prominence_ratio": first_number(
                    item_metadata.get("camera_peer_refine_prominence_ratio")
                ),
                "camera_peer_refine_overlap_seconds": first_number(
                    item_metadata.get("camera_peer_refine_overlap_seconds")
                ),
                "camera_native_relative_start_seconds": first_number(
                    item_metadata.get("camera_native_relative_start_seconds")
                ),
                "camera_native_gap_from_previous_seconds": first_number(
                    item_metadata.get("camera_native_gap_from_previous_seconds")
                ),
                "camera_block_base_seconds": first_number(
                    item_metadata.get("camera_block_base_seconds")
                ),
                "camera_base_candidate_seconds": first_number(
                    item_metadata.get("camera_base_candidate_seconds")
                ),
                "camera_base_deviation_seconds": first_number(
                    item_metadata.get("camera_base_deviation_seconds")
                ),
                "camera_block_anchor_name": item_metadata.get("camera_block_anchor_name"),
                "camera_block_anchor_z_score": first_number(
                    item_metadata.get("camera_block_anchor_z_score")
                ),
                "spanning_continuity_applied": bool(
                    item_metadata.get("spanning_continuity_applied")
                ),
                "spanning_previous_path": item_metadata.get("spanning_previous_path"),
                "spanning_gap_seconds": first_number(
                    item_metadata.get("spanning_gap_seconds")
                ),
                "spanning_old_offset_seconds": first_number(
                    item_metadata.get("spanning_old_offset_seconds")
                ),
                "spanning_new_offset_seconds": first_number(
                    item_metadata.get("spanning_new_offset_seconds")
                ),
                "skip_reason": item_metadata.get("skip_reason"),
                "error": item_metadata.get("error"),
            }
        )

    annotate_previous_clip_gaps(rows)
    return rows


def annotate_previous_clip_gaps(rows: list[dict]) -> None:
    by_camera: dict[str, list[dict]] = {}
    for row in rows:
        if row.get("status") != "ok":
            continue
        camera_name = str(row.get("camera_name") or "")
        by_camera.setdefault(camera_name, []).append(row)

    for camera_rows in by_camera.values():
        camera_rows.sort(
            key=lambda row: (
                sort_number(row.get("camera_native_relative_start_seconds")),
                sort_number(row.get("final_offset_seconds")),
                str(row.get("file_name") or ""),
            )
        )
        previous_row: dict | None = None
        for row in camera_rows:
            if previous_row is not None:
                previous_end = first_number(previous_row.get("timeline_end_seconds"))
                current_start = first_number(row.get("final_offset_seconds"))
                if previous_end is not None and current_start is not None:
                    row["previous_clip"] = previous_row.get("file_name")
                    row["timeline_gap_from_previous_seconds"] = current_start - previous_end
            previous_row = row


def build_camera_audit_summary(rows: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        if row.get("status") == "ok":
            grouped.setdefault(str(row.get("camera_name") or ""), []).append(row)

    summary: list[dict] = []
    for camera_name, camera_rows in grouped.items():
        residuals = [
            value
            for row in camera_rows
            if (value := first_number(row.get("final_vs_native_prediction_delta_seconds")))
            is not None
        ]
        z_scores = [
            value
            for row in camera_rows
            if (value := first_number(row.get("correlation_z_score"))) is not None
        ]
        gaps = [
            value
            for row in camera_rows
            if (value := first_number(row.get("timeline_gap_from_previous_seconds")))
            is not None
        ]
        drift_values = [
            value
            for row in camera_rows
            if (value := first_number(row.get("camera_clock_drift_ppm"))) is not None
        ]
        summary.append(
            {
                "camera_name": camera_name,
                "clip_count": len(camera_rows),
                "spanning_adjustment_count": sum(
                    1 for row in camera_rows if row.get("spanning_continuity_applied")
                ),
                "camera_clock_drift_ppm": median_value(drift_values),
                "camera_clock_inlier_count": first_number(
                    camera_rows[0].get("camera_clock_inlier_count")
                ),
                "camera_clock_candidate_count": first_number(
                    camera_rows[0].get("camera_clock_candidate_count")
                ),
                "min_z_score": min(z_scores) if z_scores else None,
                "median_z_score": median_value(z_scores),
                "max_abs_native_prediction_residual_seconds": (
                    max(abs(value) for value in residuals) if residuals else None
                ),
                "median_native_prediction_residual_seconds": median_value(residuals),
                "max_abs_timeline_gap_seconds": (
                    max(abs(value) for value in gaps) if gaps else None
                ),
            }
        )
    return sorted(summary, key=lambda item: item["camera_name"])


def build_camera_track_check(rows: list[dict]) -> dict:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        if row.get("status") != "ok":
            continue
        camera_name = str(row.get("camera_name") or "CAMERA")
        grouped.setdefault(camera_name, []).append(row)

    cameras: list[dict] = []
    total_overlap_count = 0
    total_overlap_seconds = 0.0
    for camera_name, camera_rows in sorted(grouped.items(), key=lambda item: item[0]):
        ordered = sorted(
            camera_rows,
            key=lambda row: (
                sort_number(row.get("camera_native_relative_start_seconds")),
                sort_number(row.get("final_offset_seconds")),
                str(row.get("file_name") or ""),
            ),
        )
        entries: list[dict] = []
        gaps: list[float] = []
        overlap_count = 0
        overlap_seconds_total = 0.0
        previous: dict | None = None
        for row in ordered:
            start = first_number(row.get("final_offset_seconds"))
            duration = first_number(row.get("duration_seconds"))
            if start is None or duration is None:
                continue
            end = start + duration
            gap = None
            overlap_seconds = 0.0
            status = "FIRST" if previous is None else "GAP"
            if previous is not None:
                previous_end = first_number(previous.get("timeline_end_seconds"))
                if previous_end is None:
                    previous_start = first_number(previous.get("final_offset_seconds"))
                    previous_duration = first_number(previous.get("duration_seconds"))
                    if previous_start is not None and previous_duration is not None:
                        previous_end = previous_start + previous_duration
                if previous_end is not None:
                    gap = start - previous_end
                    gaps.append(gap)
                    if gap < -TRACK_CHECK_OVERLAP_TOLERANCE_SECONDS:
                        status = "OVERLAP"
                        overlap_seconds = abs(gap)
                        overlap_count += 1
                        overlap_seconds_total += overlap_seconds
                    elif abs(gap) <= TRACK_CHECK_OVERLAP_TOLERANCE_SECONDS:
                        status = "TOUCH"

            entries.append(
                {
                    "file_name": row.get("file_name"),
                    "start_seconds": start,
                    "end_seconds": end,
                    "duration_seconds": duration,
                    "previous_clip": None if previous is None else previous.get("file_name"),
                    "gap_from_previous_seconds": gap,
                    "overlap_seconds": overlap_seconds,
                    "status": status,
                }
            )
            previous = row

        total_overlap_count += overlap_count
        total_overlap_seconds += overlap_seconds_total
        cameras.append(
            {
                "camera_name": camera_name,
                "clip_count": len(entries),
                "overlap_count": overlap_count,
                "total_overlap_seconds": overlap_seconds_total,
                "min_gap_seconds": min(gaps) if gaps else None,
                "max_gap_seconds": max(gaps) if gaps else None,
                "entries": entries,
            }
        )

    return {
        "overlap_tolerance_seconds": TRACK_CHECK_OVERLAP_TOLERANCE_SECONDS,
        "has_overlaps": total_overlap_count > 0,
        "total_overlap_count": total_overlap_count,
        "total_overlap_seconds": total_overlap_seconds,
        "cameras": cameras,
    }


def log_camera_track_check(track_check: dict) -> None:
    logger.info("=" * 72)
    logger.info("TRACK CHECK - validacao de ordem/gaps por camera")
    logger.info("=" * 72)
    for camera in track_check.get("cameras") or []:
        overlap_count = int(camera.get("overlap_count") or 0)
        log_fn = logger.warning if overlap_count else logger.info
        log_fn(
            "TRACK %s | clipes=%d | overlaps=%d | min_gap=%s | max_gap=%s",
            camera.get("camera_name"),
            int(camera.get("clip_count") or 0),
            overlap_count,
            format_track_check_seconds(camera.get("min_gap_seconds")),
            format_track_check_seconds(camera.get("max_gap_seconds")),
        )
        for entry in camera.get("entries") or []:
            status = str(entry.get("status") or "")
            entry_log_fn = logger.warning if status == "OVERLAP" else logger.info
            entry_log_fn(
                "  [%s] %s | %s -> %s | gap=%s | prev=%s",
                status,
                entry.get("file_name"),
                format_track_check_seconds(entry.get("start_seconds")),
                format_track_check_seconds(entry.get("end_seconds")),
                format_track_check_seconds(entry.get("gap_from_previous_seconds")),
                entry.get("previous_clip") or "-",
            )
    if track_check.get("has_overlaps"):
        logger.warning(
            "TRACK CHECK encontrou %d sobreposicao(oes), total %.6fs.",
            int(track_check.get("total_overlap_count") or 0),
            float(track_check.get("total_overlap_seconds") or 0.0),
        )
    else:
        logger.info("TRACK CHECK OK: nenhuma sobreposicao real detectada por camera.")


def build_summary(sync_results: dict, output_xml_path: Path) -> str:
    offsets = sync_results.get("offsets") or {}
    metadata = sync_results.get("metadata") or {}
    successful = {path: offset for path, offset in offsets.items() if offset is not None}
    failed = [path for path, offset in offsets.items() if offset is None]
    audit_reports = metadata.get("audit_reports") or {}
    track_check = metadata.get("track_check") or {}
    track_overlap_count = int(track_check.get("total_overlap_count") or 0)
    reference_count = int(metadata.get("reference_count") or 0)
    success_count = len(successful)
    reference_cache_hits = int(metadata.get("reference_cache_hit_features_count") or 0)
    target_cache_hits = int(metadata.get("target_cache_hit_features_count") or 0)
    auto_cache_cleanup = metadata.get("auto_cache_cleanup") or {}
    cache_auto_summary = format_auto_cache_cleanup(auto_cache_cleanup)

    lines = [
        "",
        "=" * 72,
        "SUMARIO DA SINCRONIZACAO",
        "=" * 72,
        f"Referencia : {sync_results.get('reference')}",
    ]
    if metadata.get("project_config_path"):
        lines.append(f"Config     : {metadata.get('project_config_path')}")
    lines.extend(
        [
        f"Master Ref : {metadata.get('master_reference_name', 'n/a')}",
        f"Master Trk : {metadata.get('master_reference_track_name', 'n/a')}",
        f"Ref Scope  : {metadata.get('reference_match_scope', 'n/a')}",
        f"Cam Offset : {metadata.get('camera_global_offset_seconds', 0.0)}s",
        f"Cam Ajustes: {metadata.get('camera_offset_seconds_by_name', {})}",
        f"Clock Model: {metadata.get('use_camera_clock_model', False)}",
        f"Spanning   : {metadata.get('spanning_continuity_adjustment_count', 0)} ajuste(s)",
        f"TrackCheck : {'OK' if track_overlap_count == 0 else f'{track_overlap_count} overlap(s)'}",
        f"Cache DSP  : refs {reference_cache_hits}/{reference_count} | targets {target_cache_hits}/{success_count}",
        f"Cache Auto : {cache_auto_summary}",
        f"XML        : {output_xml_path}",
        f"Audit CSV  : {audit_reports.get('csv', 'n/a')}",
        f"Audit JSON : {audit_reports.get('json', 'n/a')}",
        f"Sucesso    : {len(successful)} arquivo(s)",
        f"Falhas     : {len(failed)} arquivo(s)",
        "-" * 72,
        ]
    )

    if successful:
        lines.append("Arquivos sincronizados:")
        for path, offset in successful.items():
            lines.append(f"  [OK]    {Path(path).name} | offset {format_offset(float(offset))}")

    if failed:
        lines.append("")
        lines.append("Arquivos com falha:")
        for path in failed:
            lines.append(f"  [FAIL]  {Path(path).name}")

    if track_overlap_count:
        lines.append("")
        lines.append("Sobreposicoes detectadas no TrackCheck:")
        for camera in track_check.get("cameras") or []:
            for entry in camera.get("entries") or []:
                if entry.get("status") != "OVERLAP":
                    continue
                lines.append(
                    "  [OVERLAP] "
                    f"{camera.get('camera_name')} | {entry.get('file_name')} "
                    f"entra {format_track_check_seconds(entry.get('overlap_seconds'))} "
                    f"antes do fim de {entry.get('previous_clip')}"
                )

    lines.append("=" * 72)
    return "\n".join(lines)


def format_auto_cache_cleanup(value: object) -> str:
    if not isinstance(value, dict) or not value:
        return "n/a"
    if value.get("error"):
        return f"falhou ({value.get('error')})"
    interval = int(value.get("interval_syncs") or 1)
    if value.get("cleanup_performed"):
        removed_files = int(value.get("removed_files") or 0)
        removed_bytes = int(value.get("removed_bytes") or 0)
        return f"limpo apos cada sync | {removed_files} arquivo(s), {removed_bytes / (1024.0**3):.2f} GiB"
    completed = int(value.get("completed_since_cleanup") or 0)
    remaining = max(0, interval - completed)
    return f"limpa apos cada sync | pendente: {remaining}"


def format_offset(offset_seconds: float) -> str:
    sign = "+" if offset_seconds >= 0 else "-"
    absolute = abs(offset_seconds)
    minutes = int(absolute // 60)
    seconds = absolute % 60
    return f"{sign}{minutes:02d}:{seconds:06.3f} ({offset_seconds:.6f}s)"


def format_track_check_seconds(value: object) -> str:
    number = first_number(value)
    if number is None:
        return "n/a"
    return f"{number:.6f}s"


def sync_offset_seconds(raw_offset: object) -> float | None:
    if isinstance(raw_offset, dict):
        return first_number(
            raw_offset.get("offset"),
            raw_offset.get("offset_seconds"),
            raw_offset.get("sync_offset_seconds"),
        )
    return first_number(raw_offset)


def delta(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return left - right


def sort_number(value: object) -> float:
    number = first_number(value)
    return float("inf") if number is None else number


def first_number(*values: object) -> float | None:
    for value in values:
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def first_text(*values: object) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def median_value(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / 2.0
