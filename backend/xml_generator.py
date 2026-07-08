"""
Gerador de timeline no padrao FCP 7 XML (xmeml v4) para Adobe Premiere Pro.

O modulo converte os offsets calculados pelo motor de sincronizacao em uma
sequencia multi-track com video, audio embutido dos videos e uma faixa master
dedicada para o audio de referencia.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote
import xml.dom.minidom as minidom
import xml.etree.ElementTree as ET


DEFAULT_CLIP_DURATION_SECONDS = 60.0
DEFAULT_REFERENCE_DURATION_SECONDS = 60.0


def create_timeline_xml(
    sync_results: dict,
    output_xml_path: str | Path,
    timebase: str = "24",
) -> Path:
    """
    Cria um XML FCP 7 (xmeml version 4) aceito pelo Adobe Premiere Pro.

    Estrutura esperada em sync_results:
        {
            "reference": "caminho/para/lapela.wav",
            "offsets": {"caminho/video.mp4": 12.34},
            "metadata": {
                "reference_duration_seconds": 1800.0,
                "caminho/video.mp4": {"duration_seconds": 300.0, "lane": 1}
            }
        }

    Tambem aceita offsets enriquecidos por arquivo:
        {"caminho/video.mp4": {"offset": 12.34, "duration_seconds": 300, "lane": 1}}
    """
    fps = _parse_timebase(timebase)
    output_path = Path(output_xml_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    clips = _collect_valid_clips(sync_results, fps)
    reference_path = sync_results.get("reference")
    metadata = sync_results.get("metadata") or {}

    if not reference_path:
        raise ValueError("sync_results precisa conter a chave 'reference'.")

    if not clips:
        raise ValueError("sync_results nao contem offsets validos para gerar a timeline.")

    min_start = min(clip["offset_frames"] for clip in clips)
    shift_frames = abs(min_start) if min_start < 0 else 0

    for clip in clips:
        clip["start"] = clip["offset_frames"] + shift_frames
        clip["end"] = clip["start"] + clip["duration_frames"]

    reference_duration = _seconds_to_frames(
        _first_number(
            metadata.get("reference_duration_seconds"),
            metadata.get("duration_seconds"),
            DEFAULT_REFERENCE_DURATION_SECONDS,
        ),
        fps,
    )
    reference_start = shift_frames
    reference_end = reference_start + reference_duration
    sequence_duration = max([reference_end, *[clip["end"] for clip in clips]])

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

    clip_counter = 1
    for track_clips in _clips_grouped_by_lane(clips):
        video_track = ET.SubElement(video, "track")
        for clip in track_clips:
            _add_clipitem(
                video_track,
                clip=clip,
                fps=fps,
                file_kind="video",
                item_id=f"video-clip-{clip_counter}",
                file_id=f"file-video-{clip_counter}",
            )
            clip_counter += 1

    clip_counter = 1
    for track_clips in _clips_grouped_by_lane(clips):
        audio_track = ET.SubElement(audio, "track")
        for clip in track_clips:
            _add_clipitem(
                audio_track,
                clip=clip,
                fps=fps,
                file_kind="audio",
                item_id=f"audio-clip-{clip_counter}",
                file_id=f"file-audio-{clip_counter}",
            )
            clip_counter += 1

    reference_track = ET.SubElement(audio, "track")
    reference_clip = {
        "path": str(reference_path),
        "name": Path(str(reference_path)).name,
        "start": reference_start,
        "end": reference_end,
        "duration_frames": reference_duration,
        "in": 0,
        "out": reference_duration,
    }
    _add_clipitem(
        reference_track,
        clip=reference_clip,
        fps=fps,
        file_kind="audio",
        item_id="audio-reference-clip-1",
        file_id="file-reference-audio-1",
    )

    _write_pretty_xml(root, output_path)
    return output_path


def _collect_valid_clips(sync_results: dict, fps: int) -> list[dict]:
    offsets = sync_results.get("offsets") or {}
    metadata = sync_results.get("metadata") or {}
    clips: list[dict] = []

    for index, (path_str, raw_offset) in enumerate(offsets.items(), start=1):
        clip_data = raw_offset if isinstance(raw_offset, dict) else {}
        offset_seconds = _first_number(
            clip_data.get("offset"),
            clip_data.get("offset_seconds"),
            clip_data.get("sync_offset_seconds"),
            None if isinstance(raw_offset, dict) else raw_offset,
        )

        if offset_seconds is None:
            continue

        item_metadata = metadata.get(path_str) or {}
        duration_seconds = _first_number(
            clip_data.get("duration_seconds"),
            item_metadata.get("duration_seconds"),
            DEFAULT_CLIP_DURATION_SECONDS,
        )
        duration_frames = max(1, _seconds_to_frames(duration_seconds, fps))
        lane = _first_int(clip_data.get("lane"), item_metadata.get("lane"), index)

        clips.append(
            {
                "path": str(path_str),
                "name": Path(str(path_str)).name,
                "lane": max(1, lane),
                "offset_frames": _seconds_to_frames(offset_seconds, fps),
                "duration_frames": duration_frames,
                "in": 0,
                "out": duration_frames,
            }
        )

    return clips


def _add_clipitem(
    parent: ET.Element,
    *,
    clip: dict,
    fps: int,
    file_kind: str,
    item_id: str,
    file_id: str,
) -> ET.Element:
    clipitem = ET.SubElement(parent, "clipitem", id=item_id)
    ET.SubElement(clipitem, "name").text = clip["name"]
    ET.SubElement(clipitem, "duration").text = str(clip["duration_frames"])
    _add_rate(clipitem, fps)
    ET.SubElement(clipitem, "start").text = str(clip["start"])
    ET.SubElement(clipitem, "end").text = str(clip["end"])
    ET.SubElement(clipitem, "in").text = str(clip["in"])
    ET.SubElement(clipitem, "out").text = str(clip["out"])

    file_element = ET.SubElement(clipitem, "file", id=file_id)
    ET.SubElement(file_element, "name").text = clip["name"]
    ET.SubElement(file_element, "pathurl").text = _path_to_file_url(clip["path"])
    _add_rate(file_element, fps)
    ET.SubElement(file_element, "duration").text = str(clip["duration_frames"])
    media = ET.SubElement(file_element, "media")
    ET.SubElement(media, file_kind)

    return clipitem


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


def _clips_grouped_by_lane(clips: list[dict]) -> list[list[dict]]:
    grouped: dict[int, list[dict]] = {}
    for clip in sorted(clips, key=lambda item: (item["lane"], item["start"], item["name"])):
        grouped.setdefault(clip["lane"], []).append(clip)
    return list(grouped.values())


def _seconds_to_frames(seconds: float | int | str, fps: int) -> int:
    return int(round(float(seconds) * fps))


def _parse_timebase(timebase: str) -> int:
    fps = int(timebase)
    if fps <= 0:
        raise ValueError("timebase precisa ser um inteiro positivo.")
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


def _path_to_file_url(path_value: str | Path) -> str:
    path_text = str(path_value).replace("\\", "/")

    if path_text.startswith("file://"):
        return path_text

    if path_text.startswith("//"):
        return "file://localhost" + quote(path_text, safe="/:")

    return "file://localhost/" + quote(path_text, safe="/:")


def _write_pretty_xml(root: ET.Element, output_path: Path) -> None:
    rough_xml = ET.tostring(root, encoding="utf-8")
    pretty_xml = minidom.parseString(rough_xml).toprettyxml(
        indent="  ",
        encoding="UTF-8",
    )
    output_path.write_bytes(pretty_xml)


if __name__ == "__main__":
    sample_results = {
        "reference": r"E:\Audio\LAPELA.WAV",
        "offsets": {
            r"E:\Camera 01\C0012.MP4": 50.594830,
            r"E:\Camera 01\C0013.MP4": -337.611746,
            r"E:\Camera 02\C0030.MP4": -579.716100,
        },
        "metadata": {
            "reference_duration_seconds": 1800.0,
            r"E:\Camera 01\C0012.MP4": {"duration_seconds": 300.0, "lane": 1},
            r"E:\Camera 01\C0013.MP4": {"duration_seconds": 1320.0, "lane": 2},
            r"E:\Camera 02\C0030.MP4": {"duration_seconds": 15.0, "lane": 3},
        },
    }

    create_timeline_xml(sample_results, "temp/timeline_teste.xml", timebase="24")
