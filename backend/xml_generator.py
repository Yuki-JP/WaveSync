"""
FCP 7 XML generator (xmeml v4) for Adobe Premiere Pro.

The timeline layout is camera-oriented:
- one video track per camera/device/folder;
- better cameras are placed higher in Premiere video tracks: Vn, ..., V2, V1;
- camera native audio starts at A1 in the same quality priority order;
- deduplicated reference audios start below native camera audio.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import quote
import re
import xml.dom.minidom as minidom
import xml.etree.ElementTree as ET


DEFAULT_CLIP_DURATION_SECONDS = 60.0
DEFAULT_REFERENCE_DURATION_SECONDS = 60.0
PREMIERE_NTSC_TIMEBASE = 30
PREMIERE_NTSC_FRAME_RATE = 30000.0 / 1001.0
CAMERA_AUDIO_START_TRACK = 1
CAMERA_OVERLAP_FIX_TOLERANCE_FRAMES = 3


def create_timeline_xml(
    sync_results: dict,
    output_xml_path: str | Path,
    timebase: str = "24",
    camera_map: Mapping[str | Path, str] | None = None,
) -> Path:
    """
    Create an FCP 7 XML timeline grouped horizontally by camera.

    camera_map maps each media path to its camera/device/folder name:
        {
            "E:/01 CAMERAS/CAM 01/C0012.MP4": "CAM 01 - A7IV - VICTOR",
            "E:/01 CAMERAS/CAM 01/C0013.MP4": "CAM 01 - A7IV - VICTOR",
            "E:/01 CAMERAS/CAM 02/C0030.MP4": "CAM 02 - A6500",
        }

    If camera_map is omitted, the generator falls back to metadata fields
    camera_name/camera/device/folder, then to the parent folder name.
    """
    _ = timebase
    fps = PREMIERE_NTSC_TIMEBASE
    output_path = Path(output_xml_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    references = _collect_references(sync_results, fps)
    if not references:
        raise ValueError("sync_results must contain at least one reference path.")

    clips = _collect_valid_clips(sync_results, fps, camera_map)
    if not clips:
        raise ValueError("sync_results does not contain valid offsets.")

    absolute_zero = _absolute_timeline_zero(references, clips, sync_results)
    offsets_are_master_unified = _offsets_are_master_unified(sync_results)
    _finalize_reference_offsets(
        references,
        fps,
        absolute_zero,
        use_supplied_offsets=offsets_are_master_unified,
    )
    if not offsets_are_master_unified:
        _apply_reference_based_clip_offsets(clips, references, fps)

    shift_frames = _timeline_shift_frames(clips, references)
    for clip in clips:
        clip["start"] = clip["offset_frames"] + shift_frames
        clip["end"] = clip["start"] + clip["duration_frames"]

    for reference in references:
        reference["start"] = reference["offset_frames"] + shift_frames
        reference["end"] = reference["start"] + reference["duration_frames"]

    camera_groups = _clips_grouped_by_camera(clips)
    _enforce_non_overlapping_camera_groups(camera_groups)
    video_camera_groups = _camera_groups_for_video_tracks(camera_groups)
    audio_camera_groups = _camera_groups_for_audio_tracks(camera_groups)

    sequence_duration = max(
        *(reference["end"] for reference in references),
        *(clip["end"] for clip in clips),
    )

    root = ET.Element("xmeml", version="4")
    project = ET.SubElement(root, "project")
    ET.SubElement(project, "name").text = "Timeline Sincronizada"
    children = ET.SubElement(project, "children")

    sequence = ET.SubElement(children, "sequence", id="sequence-1")
    ET.SubElement(sequence, "name").text = "Timeline Sincronizada"
    _add_rate(sequence, fps)
    ET.SubElement(sequence, "duration").text = str(sequence_duration)
    _add_timecode(sequence, fps)

    media = ET.SubElement(sequence, "media")
    video = ET.SubElement(media, "video")
    audio = ET.SubElement(media, "audio")

    _add_camera_video_tracks(video, video_camera_groups, fps)
    _add_camera_audio_tracks(audio, audio_camera_groups, fps)
    _add_reference_audio_tracks(
        audio,
        references=references,
        first_track_number=1 + len(camera_groups),
        fps=fps,
    )

    _write_pretty_xml(root, output_path)
    return output_path


def _collect_valid_clips(
    sync_results: dict,
    fps: int,
    camera_map: Mapping[str | Path, str] | None,
) -> list[dict]:
    offsets = sync_results.get("offsets") or {}
    metadata = sync_results.get("metadata") or {}
    clips: list[dict] = []

    for index, (path_value, raw_offset) in enumerate(offsets.items(), start=1):
        clip_data = raw_offset if isinstance(raw_offset, dict) else {}
        offset_seconds = _first_number(
            clip_data.get("offset"),
            clip_data.get("offset_seconds"),
            clip_data.get("sync_offset_seconds"),
            None if isinstance(raw_offset, dict) else raw_offset,
        )
        if offset_seconds is None:
            continue

        path_text = str(path_value)
        item_metadata = metadata.get(path_text) or {}
        duration_seconds = _first_number(
            clip_data.get("repaired_duration_seconds"),
            clip_data.get("duration_seconds"),
            item_metadata.get("repaired_duration_seconds"),
            item_metadata.get("duration_seconds"),
            DEFAULT_CLIP_DURATION_SECONDS,
        )
        raw_correlation_offset_seconds = _first_number(
            clip_data.get("raw_correlation_offset_seconds"),
            clip_data.get("raw_peak_offset_seconds"),
            item_metadata.get("raw_correlation_offset_seconds"),
            item_metadata.get("raw_peak_offset_seconds"),
        )
        chosen_reference_path = _first_text(
            clip_data.get("chosen_reference_path"),
            item_metadata.get("chosen_reference_path"),
        )
        clips.append(
            {
                "path": path_text,
                "name": Path(path_text).name,
                "camera_name": _camera_name_for_path(
                    path_text,
                    item_metadata,
                    clip_data,
                    camera_map,
                    fallback_index=index,
                ),
                "offset_seconds": offset_seconds,
                "fallback_offset_seconds": offset_seconds,
                "offset_frames": _seconds_to_frames(offset_seconds, fps),
                "duration_frames": max(1, _seconds_to_frames(duration_seconds, fps)),
                "duration_seconds": duration_seconds,
                "absolute_start_time": _media_absolute_start_time(
                    path_text,
                    duration_seconds,
                    clip_data,
                    item_metadata,
                ),
                "chosen_reference_path": chosen_reference_path,
                "chosen_reference_name": _first_text(
                    clip_data.get("chosen_reference_name"),
                    item_metadata.get("chosen_reference_name"),
                ),
                "raw_correlation_offset_seconds": raw_correlation_offset_seconds,
                "audio_track_count": _audio_track_count(item_metadata, clip_data),
                "label": "Iris",
                "in": 0,
                "out": max(1, _seconds_to_frames(duration_seconds, fps)),
            }
        )

    return clips


def _collect_references(sync_results: dict, fps: int) -> list[dict]:
    metadata = sync_results.get("metadata") or {}
    raw_references = sync_results.get("references")

    if not raw_references:
        reference_path = sync_results.get("reference")
        if not reference_path:
            return []
        raw_references = [
            {
                "path": reference_path,
                "name": "Lapela 01",
                "duration_seconds": _first_number(
                    metadata.get("reference_repaired_duration_seconds"),
                    metadata.get("reference_duration_seconds"),
                    DEFAULT_REFERENCE_DURATION_SECONDS,
                ),
                "estimated_start_time": metadata.get("reference_estimated_start_time"),
                "timeline_offset_seconds": 0.0,
            }
        ]

    references_by_key: OrderedDict[str, dict] = OrderedDict()
    for index, raw_reference_value in enumerate(raw_references, start=1):
        raw_reference = raw_reference_value if isinstance(raw_reference_value, Mapping) else {}
        path_text = str(
            raw_reference.get("path")
            or raw_reference.get("reference")
            or raw_reference_value
            or ""
        )
        if not path_text:
            continue
        if _is_drift_corrected_reference(path_text):
            continue

        item_metadata = metadata.get(path_text) or {}
        duration_seconds = _first_number(
            raw_reference.get("repaired_duration_seconds"),
            raw_reference.get("duration_seconds"),
            item_metadata.get("repaired_duration_seconds"),
            item_metadata.get("duration_seconds"),
            DEFAULT_REFERENCE_DURATION_SECONDS,
        )
        offset_seconds = _first_number(
            raw_reference.get("timeline_offset_seconds"),
            raw_reference.get("offset_seconds"),
            item_metadata.get("timeline_offset_seconds"),
            item_metadata.get("offset_seconds"),
            0.0,
        )
        name = _first_text(raw_reference.get("name"), Path(path_text).stem, f"Lapela {index:02d}")
        track_name = _first_text(
            raw_reference.get("track_name"),
            raw_reference.get("lapel_name"),
            raw_reference.get("recorder_name"),
            item_metadata.get("track_name"),
            item_metadata.get("lapel_name"),
            item_metadata.get("recorder_name"),
            name,
        )

        key = _reference_dedupe_key(path_text)
        references_by_key.setdefault(
            key,
            {
                "path": path_text,
                "name": name,
                "track_name": track_name,
                "offset_seconds": offset_seconds or 0.0,
                "fallback_offset_seconds": offset_seconds or 0.0,
                "offset_frames": _seconds_to_frames(offset_seconds or 0.0, fps),
                "duration_frames": max(1, _seconds_to_frames(duration_seconds, fps)),
                "duration_seconds": duration_seconds,
                "absolute_start_time": _media_absolute_start_time(
                    path_text,
                    duration_seconds,
                    raw_reference,
                    item_metadata,
                ),
                "label": "Caribbean",
                "in": 0,
                "out": max(1, _seconds_to_frames(duration_seconds, fps)),
            },
        )

    return list(references_by_key.values())


def _is_drift_corrected_reference(path_text: str) -> bool:
    normalized = path_text.replace("\\", "/").casefold()
    return "_drift_corrected" in Path(path_text).stem.casefold() or "pluraleyes_drift_corrected" in normalized


def _reference_dedupe_key(path_text: str) -> str:
    stem = re.sub(r"_drift_corrected$", "", Path(path_text).stem, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", stem).strip().casefold()


def _absolute_timeline_zero(
    references: list[dict],
    clips: list[dict],
    sync_results: dict,
) -> float | None:
    metadata = sync_results.get("metadata") or {}
    anchor_start = _first_number(
        metadata.get("timeline_anchor_start_time"),
        metadata.get("master_reference_start_time"),
    )
    if anchor_start is not None:
        return anchor_start

    absolute_starts = [
        start_time
        for item in [*references, *clips]
        if (start_time := _first_number(item.get("absolute_start_time"))) is not None
    ]
    return min(absolute_starts) if absolute_starts else None


def _offsets_are_master_unified(sync_results: dict) -> bool:
    value = (sync_results.get("metadata") or {}).get("offsets_are_master_unified")
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes", "sim"}


def _finalize_reference_offsets(
    references: list[dict],
    fps: int,
    absolute_zero: float | None,
    *,
    use_supplied_offsets: bool,
) -> None:
    for reference in references:
        if use_supplied_offsets:
            offset_seconds = _first_number(reference.get("fallback_offset_seconds"), 0.0) or 0.0
        else:
            absolute_start = _first_number(reference.get("absolute_start_time"))
            if absolute_zero is not None and absolute_start is not None:
                offset_seconds = absolute_start - absolute_zero
            else:
                offset_seconds = _first_number(reference.get("fallback_offset_seconds"), 0.0) or 0.0

        reference["offset_seconds"] = offset_seconds
        reference["offset_frames"] = _seconds_to_frames(offset_seconds, fps)


def _apply_reference_based_clip_offsets(
    clips: list[dict],
    references: list[dict],
    fps: int,
) -> None:
    for clip in clips:
        raw_correlation_offset = _first_number(clip.get("raw_correlation_offset_seconds"))
        if raw_correlation_offset is None:
            continue

        reference = _reference_for_clip(clip, references)
        if reference is None:
            continue

        offset_seconds = (
            _first_number(reference.get("offset_seconds"), 0.0) or 0.0
        ) + _correlation_offset_to_premiere_offset(raw_correlation_offset)
        clip["offset_seconds"] = offset_seconds
        clip["offset_frames"] = _seconds_to_frames(offset_seconds, fps)


def _reference_for_clip(clip: dict, references: list[dict]) -> dict | None:
    if len(references) == 1:
        return references[0]

    references_by_key: dict[str, dict] = {}
    for reference in references:
        for key in _reference_lookup_keys(reference):
            references_by_key.setdefault(key, reference)

    lookup_values = [
        clip.get("chosen_reference_path"),
        clip.get("chosen_reference_name"),
    ]
    for value in lookup_values:
        if value is None:
            continue
        for key in _lookup_keys_for_value(str(value)):
            reference = references_by_key.get(key)
            if reference is not None:
                return reference
    return None


def _reference_lookup_keys(reference: dict) -> set[str]:
    keys = set(_lookup_keys_for_value(str(reference.get("path") or "")))
    for value in (reference.get("name"), reference.get("track_name")):
        text = str(value or "").strip()
        if text:
            keys.add(text.casefold())
    return {key for key in keys if key}


def _lookup_keys_for_value(value: str) -> set[str]:
    text = value.strip()
    if not text:
        return set()
    return {
        text.casefold(),
        Path(text).name.casefold(),
        _normalize_lookup_path(text),
    }


def _correlation_offset_to_premiere_offset(correlation_offset_seconds: float) -> float:
    return -float(correlation_offset_seconds)


def _media_absolute_start_time(
    path_text: str,
    duration_seconds: float | int | str | None,
    *metadata_sources: Mapping[str, object],
) -> float | None:
    explicit_start = _explicit_absolute_start_time(*metadata_sources)
    if explicit_start is not None:
        return explicit_start

    try:
        stat_result = Path(path_text).stat()
    except OSError:
        return None

    duration = _first_number(duration_seconds)
    if duration is not None and duration > 0:
        return stat_result.st_mtime - duration
    return min(stat_result.st_ctime, stat_result.st_mtime)


def _explicit_absolute_start_time(*metadata_sources: Mapping[str, object]) -> float | None:
    for source in metadata_sources:
        if not isinstance(source, Mapping):
            continue
        start_time = _first_number(
            source.get("absolute_start_time"),
            source.get("recording_start_time"),
            source.get("creation_start_time"),
            source.get("media_start_time"),
            source.get("start_time"),
            source.get("estimated_start_time"),
        )
        if start_time is not None:
            return start_time
    return None


def _add_camera_video_tracks(
    video_parent: ET.Element,
    camera_groups: OrderedDict[str, list[dict]],
    fps: int,
) -> None:
    clip_index = 1
    for camera_number, (camera_name, camera_clips) in enumerate(camera_groups.items(), start=1):
        track = ET.SubElement(video_parent, "track")
        ET.SubElement(track, "name").text = f"V{camera_number} - {camera_name}"
        for clip in camera_clips:
            _add_clipitem(
                track,
                clip=clip,
                fps=fps,
                media_type="video",
                item_id=f"video-clip-{clip_index}",
                file_id=f"file-video-{clip_index}",
                source_track_index=1,
            )
            clip_index += 1
        _finish_track(track)


def _add_reference_audio_track(
    audio_parent: ET.Element,
    *,
    reference: dict,
    track_number: int,
    item_index: int,
    fps: int,
) -> None:
    track = ET.SubElement(audio_parent, "track")
    ET.SubElement(track, "name").text = f"A{track_number} - {reference['name']}"
    _add_clipitem(
        track,
        clip={
            "path": reference["path"],
            "name": Path(reference["path"]).name,
            "duration_frames": reference["duration_frames"],
            "start": reference["start"],
            "end": reference["end"],
            "in": 0,
            "out": reference["duration_frames"],
            "label": reference.get("label", "Caribbean"),
        },
        fps=fps,
        media_type="audio",
        item_id=f"audio-reference-clip-{item_index}",
        file_id=f"file-reference-audio-{item_index}",
        source_track_index=1,
    )
    _finish_track(track)


def _add_reference_audio_tracks(
    audio_parent: ET.Element,
    *,
    references: list[dict],
    first_track_number: int,
    fps: int,
) -> None:
    item_index = 1
    for track_offset, (track_name, track_references) in enumerate(
        _references_grouped_by_track(references).items()
    ):
        track = ET.SubElement(audio_parent, "track")
        ET.SubElement(track, "name").text = f"A{first_track_number + track_offset} - {track_name}"

        for reference in sorted(track_references, key=lambda item: (item["start"], item["name"])):
            _add_clipitem(
                track,
                clip={
                    "path": reference["path"],
                    "name": Path(reference["path"]).name,
                    "duration_frames": reference["duration_frames"],
                    "start": reference["start"],
                    "end": reference["end"],
                    "in": 0,
                    "out": reference["duration_frames"],
                    "label": reference.get("label", "Caribbean"),
                },
                fps=fps,
                media_type="audio",
                item_id=f"audio-reference-clip-{item_index}",
                file_id=f"file-reference-audio-{item_index}",
                source_track_index=1,
            )
            item_index += 1

        _finish_track(track)


def _references_grouped_by_track(references: list[dict]) -> OrderedDict[str, list[dict]]:
    grouped: OrderedDict[str, list[dict]] = OrderedDict()
    for reference in references:
        track_name = _first_text(reference.get("track_name"), reference.get("name"), "Lapela")
        grouped.setdefault(track_name, []).append(reference)
    return grouped


def _add_camera_audio_tracks(
    audio_parent: ET.Element,
    camera_groups: OrderedDict[str, list[dict]],
    fps: int,
) -> None:
    clip_index = 1
    for camera_number, (camera_name, camera_clips) in enumerate(camera_groups.items(), start=1):
        track = ET.SubElement(audio_parent, "track")
        audio_track_number = CAMERA_AUDIO_START_TRACK + camera_number - 1
        ET.SubElement(track, "name").text = f"A{audio_track_number} - {camera_name}"
        for clip in camera_clips:
            _add_clipitem(
                track,
                clip=clip,
                fps=fps,
                media_type="audio",
                item_id=f"audio-clip-{clip_index}",
                file_id=f"file-audio-{clip_index}",
                source_track_index=1,
            )
            clip_index += 1
        _finish_track(track)


def _add_clipitem(
    parent: ET.Element,
    *,
    clip: dict,
    fps: int,
    media_type: str,
    item_id: str,
    file_id: str,
    source_track_index: int,
) -> ET.Element:
    clipitem = ET.SubElement(parent, "clipitem", id=item_id)
    ET.SubElement(clipitem, "name").text = clip["name"]
    ET.SubElement(clipitem, "duration").text = str(clip["duration_frames"])
    _add_rate(clipitem, fps)
    ET.SubElement(clipitem, "start").text = str(clip["start"])
    ET.SubElement(clipitem, "end").text = str(clip["end"])
    ET.SubElement(clipitem, "in").text = str(clip["in"])
    ET.SubElement(clipitem, "out").text = str(clip["out"])
    _add_labels(clipitem, clip.get("label"))

    file_element = ET.SubElement(clipitem, "file", id=file_id)
    ET.SubElement(file_element, "name").text = clip["name"]
    ET.SubElement(file_element, "pathurl").text = _path_to_file_url(clip["path"])
    _add_rate(file_element, fps)
    ET.SubElement(file_element, "duration").text = str(clip["duration_frames"])

    media = ET.SubElement(file_element, "media")
    ET.SubElement(media, media_type)

    source_track = ET.SubElement(clipitem, "sourcetrack")
    ET.SubElement(source_track, "mediatype").text = media_type
    ET.SubElement(source_track, "trackindex").text = str(source_track_index)
    return clipitem


def _add_labels(parent: ET.Element, label: str | None) -> None:
    if not label:
        return
    labels = ET.SubElement(parent, "labels")
    ET.SubElement(labels, "label2").text = label


def _camera_name_for_path(
    path_text: str,
    item_metadata: dict,
    clip_data: dict,
    camera_map: Mapping[str | Path, str] | None,
    *,
    fallback_index: int,
) -> str:
    mapped = _lookup_camera_map(path_text, camera_map)
    if mapped:
        return mapped

    configured = _first_text(
        clip_data.get("camera_name"),
        clip_data.get("camera"),
        clip_data.get("device"),
        clip_data.get("folder"),
        item_metadata.get("camera_name"),
        item_metadata.get("camera"),
        item_metadata.get("device"),
        item_metadata.get("folder"),
    )
    if configured:
        return configured

    return Path(path_text).parent.name or f"CAM {fallback_index:02d}"


def _lookup_camera_map(path_text: str, camera_map: Mapping[str | Path, str] | None) -> str | None:
    if not camera_map:
        return None

    path_norm = _normalize_lookup_path(path_text)
    name_norm = Path(path_text).name.casefold()
    for key, camera_name in camera_map.items():
        if not camera_name:
            continue
        key_text = str(key)
        if _normalize_lookup_path(key_text) == path_norm or key_text.casefold() == name_norm:
            return str(camera_name).strip()
    return None


def _normalize_lookup_path(path_value: str | Path) -> str:
    try:
        return str(Path(path_value).resolve()).casefold()
    except OSError:
        return str(path_value).replace("\\", "/").casefold()


def _audio_track_count(item_metadata: dict, clip_data: dict) -> int:
    return max(
        1,
        _first_int(
            clip_data.get("audio_track_count"),
            clip_data.get("audio_channels"),
            clip_data.get("channels"),
            item_metadata.get("audio_track_count"),
            item_metadata.get("audio_channels"),
            item_metadata.get("channels"),
            1,
        ),
    )


def _timeline_shift_frames(clips: list[dict], references: list[dict]) -> int:
    offsets = [clip["offset_frames"] for clip in clips]
    offsets.extend(reference["offset_frames"] for reference in references)
    min_offset_frames = min(offsets)
    return abs(min_offset_frames) if min_offset_frames < 0 else 0


def _clips_grouped_by_camera(clips: list[dict]) -> OrderedDict[str, list[dict]]:
    grouped: OrderedDict[str, list[dict]] = OrderedDict()
    for clip in clips:
        grouped.setdefault(clip["camera_name"], []).append(clip)

    for camera_clips in grouped.values():
        camera_clips.sort(key=lambda item: (item["start"], item["name"]))

    return grouped


def _camera_groups_for_video_tracks(
    camera_groups: OrderedDict[str, list[dict]],
) -> OrderedDict[str, list[dict]]:
    """
    Return bottom-to-top video order.

    Premiere shows higher-numbered video tracks above lower-numbered tracks, so
    the best camera must be emitted last: V1 is lower priority, Vn is highest.
    """
    return OrderedDict(
        sorted(
            camera_groups.items(),
            key=lambda item: (
                _camera_quality_score(item[0]),
                _camera_original_order(camera_groups, item[0]),
            ),
        )
    )


def _camera_groups_for_audio_tracks(
    camera_groups: OrderedDict[str, list[dict]],
) -> OrderedDict[str, list[dict]]:
    """Return top-to-bottom native camera audio order: best camera on A1."""
    return OrderedDict(
        sorted(
            camera_groups.items(),
            key=lambda item: (
                -_camera_quality_score(item[0]),
                _camera_original_order(camera_groups, item[0]),
            ),
        )
    )


def _camera_original_order(camera_groups: OrderedDict[str, list[dict]], camera_name: str) -> int:
    for index, name in enumerate(camera_groups):
        if name == camera_name:
            return index
    return len(camera_groups)


def _camera_quality_score(camera_name: str) -> int:
    """
    Heuristic priority for wedding cameras based on known device names.

    This is intentionally simple and metadata-free: it uses folder/camera names
    already selected by the user, which is enough for the current workflow.
    """
    normalized = _normalize_camera_quality_text(camera_name)
    rules: list[tuple[tuple[str, ...], int]] = [
        (("fx3", "fx30", "a7siii", "a7s iii"), 110),
        (("a7iv", "a7 iv", "alpha 7 iv"), 100),
        (("a7iii", "a7 iii", "a7s", "a7c", "a7 "), 95),
        (("a6700", "a6600", "a6500", "a6400", "a6300"), 90),
        (("zve10", "zv e10", "zv-e10"), 80),
        (("osmo pocket", "pocket 3", "op3"), 50),
        (("osmo action", "action", "oa5", "oa5p"), 45),
        (("dji",), 40),
    ]
    for tokens, score in rules:
        if any(token in normalized for token in tokens):
            return score
    return 60


def _normalize_camera_quality_text(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", value.casefold())
    collapsed = re.sub(r"\s+", " ", text).strip()
    return f" {collapsed} "


def _enforce_non_overlapping_camera_groups(
    camera_groups: OrderedDict[str, list[dict]],
) -> None:
    """
    Mantem clips de uma mesma camera na mesma track sem sobreposicao.

    Se houver uma micro-sobreposicao por arredondamento ou por pico instavel,
    o clip seguinte encosta no fim do anterior. Sobreposicoes grandes ficam
    intactas para nao transformar um erro de sync em efeito cascata/escada.
    """
    for camera_clips in camera_groups.values():
        previous_end: int | None = None
        for clip in camera_clips:
            if previous_end is not None and clip["start"] < previous_end:
                overlap_frames = previous_end - clip["start"]
                if overlap_frames > CAMERA_OVERLAP_FIX_TOLERANCE_FRAMES:
                    previous_end = max(previous_end, clip["end"])
                    continue
                clip["start"] = previous_end
                clip["end"] = clip["start"] + clip["duration_frames"]
            previous_end = clip["end"]


def _split_overlapping_clips(clips: list[dict]) -> list[list[dict]]:
    lanes: list[list[dict]] = []
    lane_ends: list[int] = []

    for clip in sorted(clips, key=lambda item: (item["start"], item["end"], item["name"])):
        for lane_index, lane_end in enumerate(lane_ends):
            if clip["start"] >= lane_end:
                lanes[lane_index].append(clip)
                lane_ends[lane_index] = clip["end"]
                break
        else:
            lanes.append([clip])
            lane_ends.append(clip["end"])

    return lanes


def _add_track_name(track: ET.Element, name: str, lane_index: int) -> None:
    suffix = "" if lane_index == 1 else f" Sub {lane_index}"
    ET.SubElement(track, "name").text = f"{name}{suffix}"


def _add_rate(parent: ET.Element, fps: int) -> ET.Element:
    rate = ET.SubElement(parent, "rate")
    ET.SubElement(rate, "timebase").text = str(fps)
    ET.SubElement(rate, "ntsc").text = "TRUE"
    return rate


def _add_timecode(parent: ET.Element, fps: int) -> ET.Element:
    timecode = ET.SubElement(parent, "timecode")
    _add_rate(timecode, fps)
    ET.SubElement(timecode, "string").text = "00:00:00:00"
    ET.SubElement(timecode, "frame").text = "0"
    ET.SubElement(timecode, "displayformat").text = "NDF"
    return timecode


def _finish_track(track: ET.Element) -> None:
    ET.SubElement(track, "enabled").text = "TRUE"
    ET.SubElement(track, "locked").text = "FALSE"


def _seconds_to_frames(seconds: float | int | str, fps: int) -> int:
    return int(round(float(seconds) * _effective_frame_rate(fps)))


def _effective_frame_rate(fps: int) -> float:
    if fps == PREMIERE_NTSC_TIMEBASE:
        return PREMIERE_NTSC_FRAME_RATE
    return float(fps)


def _parse_timebase(timebase: str) -> int:
    fps = int(timebase)
    if fps <= 0:
        raise ValueError("timebase must be a positive integer.")
    return fps


def _first_number(*values: object) -> float | None:
    for value in values:
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _first_int(*values: object) -> int:
    number = _first_number(*values)
    return 1 if number is None else int(number)


def _first_text(*values: object) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _path_to_file_url(path_value: str | Path) -> str:
    path_text = str(path_value).replace("\\", "/")
    if path_text.startswith("file://"):
        return path_text
    if path_text.startswith("//"):
        return "file://localhost" + quote(path_text, safe="/")
    return "file://localhost/" + quote(path_text, safe="/")


def _missing_paths(sync_results: dict) -> list[str]:
    paths = [sync_results.get("reference")]
    paths.extend((sync_results.get("offsets") or {}).keys())
    return [str(path) for path in paths if path and not Path(str(path)).exists()]


def _write_pretty_xml(root: ET.Element, output_path: Path) -> None:
    raw_xml = ET.tostring(root, encoding="utf-8")
    pretty_xml = minidom.parseString(raw_xml).toprettyxml(indent="  ", encoding="UTF-8")
    output_path.write_bytes(pretty_xml)


if __name__ == "__main__":
    sample_results = {
        "reference": r"E:\Audio\LAPELA.WAV",
        "offsets": {
            r"E:\Camera 01\C0012.MP4": 50.594830,
            r"E:\Camera 01\C0013.MP4": 350.594830,
            r"E:\Camera 02\C0030.MP4": -579.716100,
        },
        "metadata": {
            "reference_duration_seconds": 1800.0,
            r"E:\Camera 01\C0012.MP4": {"duration_seconds": 300.0},
            r"E:\Camera 01\C0013.MP4": {"duration_seconds": 1320.0},
            r"E:\Camera 02\C0030.MP4": {"duration_seconds": 15.0},
        },
    }
    sample_camera_map = {
        r"E:\Camera 01\C0012.MP4": "CAM 01 - A7IV - VICTOR",
        r"E:\Camera 01\C0013.MP4": "CAM 01 - A7IV - VICTOR",
        r"E:\Camera 02\C0030.MP4": "CAM 02 - A6500",
    }

    xml_path = create_timeline_xml(
        sample_results,
        "temp/timeline_teste.xml",
        timebase="24",
        camera_map=sample_camera_map,
    )
    print(f"XML gerado em: {xml_path}")

    missing = _missing_paths(sample_results)
    if missing:
        print("Aviso: este XML de exemplo aponta para arquivos que nao existem neste computador:")
        for path in missing:
            print(f"  - {path}")
        print("Use create_timeline_xml() com os caminhos reais vindos do sync_results.")
