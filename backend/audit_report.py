"""Audit, summary, and track validation helpers for the sync pipeline."""

from __future__ import annotations

import csv
import json
import logging
from datetime import datetime
from pathlib import Path


TRACK_CHECK_OVERLAP_TOLERANCE_SECONDS = 1.0 / 30.0
REFERENCE_COVERAGE_MIN_SECONDS = 10.0
REFERENCE_COVERAGE_MIN_RATIO = 0.05
SYNC_QUALITY_LARGE_DSP_DELTA_SECONDS = 10.0
SYNC_QUALITY_WEAK_PARTIAL_COVERAGE_RATIO = 0.25
SYNC_QUALITY_WEAK_PARTIAL_COVERAGE_SECONDS = 90.0

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
    "sync_quality_status",
    "sync_quality_reasons",
    "reference_coverage_status",
    "reference_coverage_seconds",
    "reference_coverage_ratio",
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
    "camera_block_anchor_is_invisible",
    "camera_block_anchor_path",
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
    sync_quality = annotate_sync_quality(rows, sync_results.get("references") or [])
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
    metadata["sync_quality"] = sync_quality
    json_payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "metadata": metadata,
        "references": sync_results.get("references") or [],
        "camera_summary": camera_summary,
        "track_check": track_check,
        "sync_quality": sync_quality,
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
                "camera_block_anchor_is_invisible": bool(
                    item_metadata.get("camera_block_anchor_is_invisible")
                ),
                "camera_block_anchor_path": item_metadata.get("camera_block_anchor_path"),
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



def annotate_sync_quality(rows: list[dict], references: list[dict]) -> dict:
    intervals = reference_coverage_intervals(references)
    issues: list[dict] = []
    counts = {"OK": 0, "ATENCAO": 0, "RISCO_ALTO": 0, "N_D": 0}

    for row in rows:
        if row.get("status") != "ok":
            row["sync_quality_status"] = "N_D"
            row["sync_quality_reasons"] = row.get("skip_reason") or "clipe_nao_sincronizado"
            row["reference_coverage_status"] = "not_synced"
            row["reference_coverage_seconds"] = None
            row["reference_coverage_ratio"] = None
            counts["N_D"] += 1
            continue

        coverage = reference_coverage_for_row(row, intervals)
        row["reference_coverage_status"] = coverage["status"]
        row["reference_coverage_seconds"] = coverage["overlap_seconds"]
        row["reference_coverage_ratio"] = coverage["coverage_ratio"]

        status, reasons = classify_sync_quality(row, coverage)
        row["sync_quality_status"] = status
        row["sync_quality_reasons"] = "; ".join(reasons) if reasons else "ok"
        counts[status] = counts.get(status, 0) + 1
        if status != "OK":
            issues.append(
                {
                    "status": status,
                    "file_name": row.get("file_name"),
                    "camera_name": row.get("camera_name"),
                    "reasons": reasons,
                    "reference_coverage_status": coverage["status"],
                    "reference_coverage_seconds": coverage["overlap_seconds"],
                    "reference_coverage_ratio": coverage["coverage_ratio"],
                    "final_offset_seconds": row.get("final_offset_seconds"),
                    "timeline_end_seconds": row.get("timeline_end_seconds"),
                    "sync_method": row.get("sync_method"),
                }
            )

    high_risk_count = counts.get("RISCO_ALTO", 0)
    attention_count = counts.get("ATENCAO", 0)
    blocking_issues = [
        issue for issue in issues if is_blocking_sync_quality_issue(issue)
    ]
    if high_risk_count:
        overall_status = f"RISCO_ALTO ({high_risk_count})"
    elif attention_count:
        overall_status = f"ATENCAO ({attention_count})"
    else:
        overall_status = "OK"

    return {
        "overall_status": overall_status,
        "counts": counts,
        "issue_count": len(issues),
        "high_risk_count": high_risk_count,
        "attention_count": attention_count,
        "blocking_issue_count": len(blocking_issues),
        "blocking_issues": blocking_issues,
        "reference_coverage_min_seconds": REFERENCE_COVERAGE_MIN_SECONDS,
        "reference_coverage_min_ratio": REFERENCE_COVERAGE_MIN_RATIO,
        "weak_partial_coverage_ratio": SYNC_QUALITY_WEAK_PARTIAL_COVERAGE_RATIO,
        "weak_partial_coverage_seconds": SYNC_QUALITY_WEAK_PARTIAL_COVERAGE_SECONDS,
        "large_dsp_delta_seconds": SYNC_QUALITY_LARGE_DSP_DELTA_SECONDS,
        "issues": issues,
    }


def is_blocking_sync_quality_issue(issue: dict) -> bool:
    if issue.get("status") != "RISCO_ALTO":
        return False
    reasons = [str(reason) for reason in (issue.get("reasons") or [])]
    if not reasons:
        return True
    return any(not reason.startswith("referencia_sem_cobertura:") for reason in reasons)


def reference_coverage_intervals(references: list[dict]) -> list[dict]:
    intervals: list[dict] = []
    for reference in references:
        start = first_number(reference.get("timeline_offset_seconds"))
        duration = first_number(
            reference.get("repaired_duration_seconds"),
            reference.get("duration_seconds"),
        )
        if start is None or duration is None:
            continue
        end = start + duration
        intervals.append(
            {
                "start": start,
                "end": end,
                "duration": duration,
                "name": reference.get("name"),
                "track_name": reference.get("track_name"),
                "path": reference.get("path"),
            }
        )
    return sorted(intervals, key=lambda item: (item["start"], item["end"]))


def reference_coverage_for_row(row: dict, intervals: list[dict]) -> dict:
    start = first_number(row.get("final_offset_seconds"))
    duration = first_number(row.get("duration_seconds"))
    if start is None or duration is None:
        return {
            "status": "unknown",
            "overlap_seconds": None,
            "coverage_ratio": None,
        }
    if not intervals:
        return {
            "status": "no_references",
            "overlap_seconds": None,
            "coverage_ratio": None,
        }

    end = start + duration
    earliest_start = min(interval["start"] for interval in intervals)
    latest_end = max(interval["end"] for interval in intervals)
    overlap_seconds = 0.0
    for interval in intervals:
        overlap_seconds = max(
            overlap_seconds,
            min(end, interval["end"]) - max(start, interval["start"]),
        )
    overlap_seconds = max(0.0, overlap_seconds)
    coverage_ratio = overlap_seconds / max(duration, 1e-9)
    required_overlap = min(
        REFERENCE_COVERAGE_MIN_SECONDS,
        max(1.0, duration * REFERENCE_COVERAGE_MIN_RATIO),
    )

    if overlap_seconds <= 0.0:
        if end <= earliest_start:
            status = "before_reference_start"
        elif start >= latest_end:
            status = "after_reference_end"
        else:
            status = "outside_reference_coverage"
    elif overlap_seconds < required_overlap:
        status = "partial_reference_coverage"
    elif start < earliest_start:
        status = "covered_after_camera_preroll"
    elif end > latest_end + REFERENCE_COVERAGE_MIN_SECONDS:
        status = "covered_with_tail_after_reference"
    else:
        status = "covered"

    return {
        "status": status,
        "overlap_seconds": overlap_seconds,
        "coverage_ratio": coverage_ratio,
    }


def classify_sync_quality(row: dict, coverage: dict) -> tuple[str, list[str]]:
    reasons: list[str] = []
    high_risk = False
    attention = False

    coverage_status = str(coverage.get("status") or "unknown")
    coverage_seconds = first_number(coverage.get("overlap_seconds"))
    coverage_ratio = first_number(coverage.get("coverage_ratio"))
    peer_confirmed = is_peer_confirmed(row)
    final_vs_individual = first_number(row.get("individual_vs_final_delta_seconds"))
    sync_method = str(row.get("sync_method") or "")
    local_delta = first_number(row.get("camera_local_refine_delta_seconds"))
    local_z = first_number(row.get("camera_local_refine_z_score"))
    peer_delta = first_number(row.get("camera_peer_refine_delta_seconds"))
    peer_z = first_number(row.get("camera_peer_refine_z_score"))
    final_z = first_number(row.get("correlation_z_score"))
    clock_inliers = first_number(row.get("camera_clock_inlier_count"))

    local_or_peer_confirmed = (
        local_delta is not None
        and abs(local_delta) <= 0.5
        and local_z is not None
        and local_z >= 1.15
    ) or (
        peer_delta is not None
        and abs(peer_delta) <= 0.8
        and peer_z is not None
        and peer_z >= 2.5
    )

    outside_reference_statuses = {
        "after_reference_end",
        "before_reference_start",
        "outside_reference_coverage",
        "no_references",
    }
    partial_reference_statuses = {
        "partial_reference_coverage",
        "covered_after_camera_preroll",
        "covered_with_tail_after_reference",
    }

    if coverage_status in outside_reference_statuses:
        attention = True
        if peer_confirmed:
            reasons.append(f"sem_lapela_confirmado_por_camera:{coverage_status}")
        else:
            reasons.append(f"referencia_sem_cobertura:{coverage_status}")
    elif coverage_status in partial_reference_statuses:
        attention = True
        reasons.append(f"cobertura_parcial:{coverage_status}")
        weak_partial_coverage = (
            coverage_ratio is not None
            and coverage_ratio < SYNC_QUALITY_WEAK_PARTIAL_COVERAGE_RATIO
            and coverage_seconds is not None
            and coverage_seconds < SYNC_QUALITY_WEAK_PARTIAL_COVERAGE_SECONDS
        )
        weakly_supported_anchor = (
            sync_method
            in {"camera_block_anchor_individual_dsp", "camera_block_individual_dsp"}
            and (clock_inliers is None or clock_inliers < 1)
            and not local_or_peer_confirmed
        )
        if weak_partial_coverage and weakly_supported_anchor:
            high_risk = True
            reasons.append(
                "cobertura_parcial_fraca_sem_confirmacao:"
                f"{coverage_seconds:.3f}s/{coverage_ratio:.3f}"
            )

    if (
        final_vs_individual is not None
        and abs(final_vs_individual) > SYNC_QUALITY_LARGE_DSP_DELTA_SECONDS
        and "native" in sync_method
        and not local_or_peer_confirmed
    ):
        if (
            coverage_status not in outside_reference_statuses
            and final_z is not None
            and final_z >= 8.0
        ):
            high_risk = True
        else:
            attention = True
        reasons.append(
            "offset_final_diverge_muito_do_dsp:"
            f"{final_vs_individual:.3f}s"
        )

    if final_z is not None and final_z < 4.0:
        high_risk = True
        reasons.append(f"z_score_baixo:{final_z:.2f}")

    if high_risk:
        return "RISCO_ALTO", reasons
    if attention:
        return "ATENCAO", reasons
    return "OK", reasons

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
    sync_quality = metadata.get("sync_quality") or {}
    sync_quality_status = sync_quality.get("overall_status", "n/a")
    sync_guard_count = int(sync_quality.get("blocking_issue_count") or 0)
    track_overlap_count = int(track_check.get("total_overlap_count") or 0)
    reference_count = int(metadata.get("reference_count") or 0)
    success_count = len(successful)
    reference_cache_hits = int(metadata.get("reference_cache_hit_features_count") or 0)
    target_cache_hits = int(metadata.get("target_cache_hit_features_count") or 0)
    auto_cache_cleanup = metadata.get("auto_cache_cleanup") or {}
    cache_auto_summary = format_auto_cache_cleanup(auto_cache_cleanup)
    invisible_anchor_summary = format_invisible_anchor_support(
        metadata.get("invisible_anchor_support") or {}
    )

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
        f"Ancora Aux : {invisible_anchor_summary}",
        f"Spanning   : {metadata.get('spanning_continuity_adjustment_count', 0)} ajuste(s)",
        f"TrackCheck : {'OK' if track_overlap_count == 0 else f'{track_overlap_count} overlap(s)'}",
        f"SyncCheck  : {sync_quality_status}",
        f"SyncGuard  : {'BLOQUEIO' if sync_guard_count else 'OK'}",
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

    sync_quality_issues = sync_quality.get("issues") or []
    if sync_quality_issues:
        lines.append("")
        lines.append("Alertas de qualidade do sync:")
        for issue in sync_quality_issues[:8]:
            reasons = ", ".join(issue.get("reasons") or [])
            lines.append(
                "  "
                f"[{issue.get('status')}] {issue.get('file_name')} | "
                f"{issue.get('camera_name')} | {reasons}"
            )
        if len(sync_quality_issues) > 8:
            lines.append(
                f"  ... mais {len(sync_quality_issues) - 8} alerta(s) no audit JSON/CSV"
            )
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



def format_invisible_anchor_support(value: object) -> str:
    if not isinstance(value, dict) or not value:
        return "n/a"
    if not value.get("enabled"):
        return "desativada"
    if not value.get("triggered"):
        return "nao necessario"
    cameras = value.get("cameras_requiring_support") or []
    camera_count = len(cameras) if isinstance(cameras, list) else 0
    accepted_count = int(value.get("accepted_count") or 0)
    blocked_cameras = value.get("blocked_cameras") or []
    blocked_count = len(blocked_cameras) if isinstance(blocked_cameras, list) else 0
    if blocked_count:
        return f"bloqueada ({blocked_count} camera(s) sem confirmacao segura)"
    if value.get("used"):
        return f"usada ({accepted_count} apoio(s), {camera_count} camera(s))"
    return f"tentada sem apoio util ({camera_count} camera(s))"


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




def is_peer_confirmed(row: dict) -> bool:
    method = str(row.get("sync_method") or "")
    if "peer" not in method:
        return False
    peer_z = first_number(row.get("camera_peer_refine_z_score"))
    peer_prominence = first_number(row.get("camera_peer_refine_prominence_ratio"))
    peer_overlap = first_number(row.get("camera_peer_refine_overlap_seconds"))
    return (
        peer_z is not None
        and peer_z >= 8.0
        and peer_prominence is not None
        and peer_prominence >= 2.0
        and peer_overlap is not None
        and peer_overlap >= 10.0
    )


def median_value(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / 2.0
