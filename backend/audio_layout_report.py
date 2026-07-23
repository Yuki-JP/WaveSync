"""Audio layout diagnostics for generated Premiere XML files."""

from __future__ import annotations

import csv
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote, urlparse


AUDIO_LAYOUT_SUFFIX = "_audio_layout"


def write_audio_layout_reports(xml_path: str | Path, output_base: str | Path | None = None) -> dict:
    """Write CSV/JSON/TXT reports describing how audio channels are mapped in XML."""
    xml_path = Path(xml_path)
    report = build_audio_layout_report(xml_path)

    if output_base is None:
        output_base_path = xml_path.with_name(f"{xml_path.stem}{AUDIO_LAYOUT_SUFFIX}")
    else:
        output_base_path = Path(output_base)

    csv_path = output_base_path.with_suffix(".csv")
    json_path = output_base_path.with_suffix(".json")
    txt_path = output_base_path.with_suffix(".txt")
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    _write_csv(csv_path, report["rows"])
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    txt_path.write_text(_format_text_report(report), encoding="utf-8")

    return {
        "status": report["status"],
        "issue_count": len(report["issues"]),
        "csv": str(csv_path),
        "json": str(json_path),
        "txt": str(txt_path),
    }


def build_audio_layout_report(xml_path: str | Path) -> dict:
    xml_path = Path(xml_path)
    root = ET.parse(xml_path).getroot()
    file_elements_by_id = _file_elements_by_id(root)
    sequence = root.find(".//sequence")
    audio = sequence.find("media/audio") if sequence is not None else None
    if audio is None:
        raise ValueError(f"XML sem sequence/media/audio: {xml_path}")

    rows: list[dict] = []
    track_summaries: list[dict] = []
    issues: list[dict] = []

    for track_index, track in enumerate(audio.findall("track"), start=1):
        track_name = _text(track.find("name")) or f"A{track_index}"
        track_output_channel_index = _int(_text(track.find("outputchannelindex")))
        premiere_track_type = track.get("premiereTrackType") or ""
        current_exploded_track_index = track.get("currentExplodedTrackIndex") or ""
        total_exploded_track_count = track.get("totalExplodedTrackCount") or ""
        lane_info = _parse_audio_lane(track_name)
        clipitems = track.findall("clipitem")
        track_rows: list[dict] = []

        for clip_position, clipitem in enumerate(clipitems, start=1):
            file_element = clipitem.find("file")
            file_id = file_element.get("id") if file_element is not None else ""
            resolved_file_element = file_element
            if file_id and (resolved_file_element is None or resolved_file_element.find("media") is None):
                resolved_file_element = file_elements_by_id.get(file_id, resolved_file_element)
            audio_node = resolved_file_element.find("media/audio") if resolved_file_element is not None else None
            source_track_index = _int(_text(clipitem.find("sourcetrack/trackindex")))
            channel_count = _int(_text(audio_node.find("channelcount"))) if audio_node is not None else None
            layout = _text(audio_node.find("layout")) if audio_node is not None else ""
            description = _text(audio_node.find("channeldescription")) if audio_node is not None else ""
            audio_channels = _audio_channel_specs(audio_node)
            link_specs = _clipitem_link_specs(clipitem)
            link_group_indexes = _clipitem_link_group_indexes(clipitem)
            file_path = _pathurl_to_path(_text(resolved_file_element.find("pathurl"))) if resolved_file_element is not None else ""
            has_pan_or_output = _has_pan_or_output_assignment(clipitem) or _has_pan_or_output_assignment(track)

            row = {
                "track_index": track_index,
                "track_name": track_name,
                "track_kind": lane_info["kind"],
                "camera_or_track": lane_info["base_name"],
                "lane": lane_info["lane"],
                "expected_source_track_index": lane_info["expected_source_track_index"],
                "clip_position": clip_position,
                "clip_id": clipitem.get("id") or "",
                "file_id": file_id,
                "premiere_channel_type": clipitem.get("premiereChannelType") or "",
                "clip_name": _text(clipitem.find("name")),
                "start": _text(clipitem.find("start")),
                "end": _text(clipitem.find("end")),
                "source_track_index": source_track_index,
                "file_channel_count": channel_count,
                "file_layout": layout,
                "file_channel_description": description,
                "xml_audio_channels": ";".join(audio_channels),
                "xml_links": ";".join(link_specs),
                "link_group_indexes": link_group_indexes,
                "track_output_channel_index": track_output_channel_index,
                "premiere_track_type": premiere_track_type,
                "current_exploded_track_index": current_exploded_track_index,
                "total_exploded_track_count": total_exploded_track_count,
                "has_pan_or_output_assignment": has_pan_or_output,
                "file_path": file_path,
            }
            track_rows.append(row)
            rows.append(row)

            expected = lane_info["expected_source_track_index"]
            if expected and source_track_index != expected:
                issues.append(
                    _issue(
                        "ERRO",
                        "source_track_inesperado",
                        track_name,
                        row["clip_name"],
                        f"lane {lane_info['lane']} esperava sourcetrack {expected}, mas XML usa {source_track_index}",
                    )
                )
            if channel_count and channel_count >= 2 and row["premiere_channel_type"].casefold() != "stereo":
                issues.append(
                    _issue(
                        "ATENCAO",
                        "premiere_channel_type_ausente",
                        track_name,
                        row["clip_name"],
                        "Midia estereo sem premiereChannelType=stereo; o Premiere pode importar este item como mono.",
                    )
                )
            if expected and channel_count and channel_count < expected:
                issues.append(
                    _issue(
                        "ERRO",
                        "canal_origem_inexistente",
                        track_name,
                        row["clip_name"],
                        f"XML tenta usar sourcetrack {expected}, mas a midia declara {channel_count} canal(is)",
                    )
                )

        summary = _track_summary(track_index, track_name, lane_info, track_rows)
        track_summaries.append(summary)
        if summary["is_split_camera_lane"]:
            expected_exploded = str(int(summary["expected_source_track_index"]) - 1)
            has_track_stereo = (
                summary["premiere_track_types"] == ["Stereo"]
                and summary["current_exploded_track_indexes"] == [expected_exploded]
                and summary["total_exploded_track_counts"] == ["2"]
            )
            if not has_track_stereo:
                issues.append(
                    _issue(
                        "ATENCAO",
                        "track_estereo_explodido_ausente",
                        track_name,
                        "",
                        "Lane E/D sem premiereTrackType=Stereo/currentExplodedTrackIndex; o Premiere pode criar dois clipes de audio em vez de um stereo correto.",
                    )
                )

        if summary["is_split_camera_lane"] and not summary["has_stereo_group_link"]:
            issues.append(
                _issue(
                    "ATENCAO",
                    "lane_sem_link_estereo",
                    track_name,
                    "",
                    "O XML escolhe o canal de origem correto, mas ainda nao declara link/groupindex de par estereo para este lane.",
                )
            )

    split_pairs = _build_split_pair_summary(track_summaries)
    status = _overall_status(issues)
    cause_hints = _cause_hints(issues, split_pairs)

    return {
        "xml": str(xml_path),
        "status": status,
        "track_count": len(track_summaries),
        "clipitem_count": len(rows),
        "track_summaries": track_summaries,
        "split_pairs": split_pairs,
        "issues": issues,
        "cause_hints": cause_hints,
        "rows": rows,
    }


def _file_elements_by_id(root: ET.Element) -> dict[str, ET.Element]:
    file_elements: dict[str, ET.Element] = {}
    for file_element in root.findall(".//file"):
        file_id = file_element.get("id")
        if file_id and file_element.find("media") is not None:
            file_elements.setdefault(file_id, file_element)
    return file_elements

def _write_csv(path: Path, rows: list[dict]) -> None:
    fieldnames = [
        "track_index",
        "track_name",
        "track_kind",
        "camera_or_track",
        "lane",
        "expected_source_track_index",
        "clip_position",
        "clip_id",
        "file_id",
        "premiere_channel_type",
        "clip_name",
        "start",
        "end",
        "source_track_index",
        "file_channel_count",
        "file_layout",
        "file_channel_description",
        "xml_audio_channels",
        "xml_links",
        "link_group_indexes",
        "track_output_channel_index",
        "premiere_track_type",
        "current_exploded_track_index",
        "total_exploded_track_count",
        "has_pan_or_output_assignment",
        "file_path",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _format_text_report(report: dict) -> str:
    lines = [
        "WaveSync - AudioLayout Report",
        "=" * 72,
        f"XML       : {report['xml']}",
        f"Status    : {report['status']}",
        f"Tracks    : {report['track_count']}",
        f"Clipitems : {report['clipitem_count']}",
        "",
        "Tracks da sequence",
        "-" * 72,
    ]
    for track in report["track_summaries"]:
        lines.append(
            f"A{track['track_index']:02d} | {track['track_name']} | clips={track['clip_count']} | "
            f"source={track['source_track_indexes']} | canais={track['file_channel_counts']} | "
            f"output={track['track_output_channel_indexes']} | premiere={track['premiere_track_types']} | "
            f"clipPremiere={track['premiere_channel_types']} | "
            f"exploded={track['current_exploded_track_indexes']}/{track['total_exploded_track_counts']} | "
            f"group={track['link_group_indexes']} | stereo_link={track['has_stereo_group_link']}"
        )

    if report["split_pairs"]:
        lines.extend(["", "Pares E/D detectados", "-" * 72])
        for pair in report["split_pairs"]:
            lines.append(
                f"{pair['base_name']} | lanes={pair['lanes']} | sources={pair['source_track_indexes']} | "
                f"group={pair['link_group_indexes']} | completo={pair['complete']}"
            )
        if any(pair["complete"] for pair in report["split_pairs"]):
            lines.extend(
                [
                    "",
                    "Observacao Premiere",
                    "-" * 72,
                    "Modo compativel com PluralEyes/FCP7: cameras estereo aparecem como par E/D linkado.",
                    "Com premiereChannelType=stereo, o Premiere deve interpretar o clip como Estereo no painel Modificar clipe.",
                ]
            )

    if report["issues"]:
        lines.extend(["", "Alertas", "-" * 72])
        for issue in report["issues"]:
            clip = f" | {issue['clip_name']}" if issue.get("clip_name") else ""
            lines.append(f"[{issue['severity']}] {issue['code']} | {issue['track_name']}{clip} | {issue['message']}")

    if report["cause_hints"]:
        lines.extend(["", "Leitura provavel", "-" * 72])
        lines.extend(f"- {hint}" for hint in report["cause_hints"])

    return "\n".join(lines) + "\n"


def _track_summary(track_index: int, track_name: str, lane_info: dict, rows: list[dict]) -> dict:
    link_group_indexes = _sorted_unique(
        index
        for row in rows
        for index in row["link_group_indexes"]
    )
    return {
        "track_index": track_index,
        "track_name": track_name,
        "kind": lane_info["kind"],
        "base_name": lane_info["base_name"],
        "lane": lane_info["lane"],
        "expected_source_track_index": lane_info["expected_source_track_index"],
        "is_split_camera_lane": bool(lane_info["expected_source_track_index"]),
        "clip_count": len(rows),
        "source_track_indexes": _sorted_unique(row["source_track_index"] for row in rows),
        "file_channel_counts": _sorted_unique(row["file_channel_count"] for row in rows),
        "file_layouts": _sorted_unique(row["file_layout"] for row in rows),
        "track_output_channel_indexes": _sorted_unique(row["track_output_channel_index"] for row in rows),
        "premiere_track_types": _sorted_unique(row["premiere_track_type"] for row in rows),
        "premiere_channel_types": _sorted_unique(row["premiere_channel_type"] for row in rows),
        "current_exploded_track_indexes": _sorted_unique(row["current_exploded_track_index"] for row in rows),
        "total_exploded_track_counts": _sorted_unique(row["total_exploded_track_count"] for row in rows),
        "link_group_indexes": link_group_indexes,
        "has_stereo_group_link": bool(link_group_indexes),
        "has_pan_or_output_assignment": any(row["has_pan_or_output_assignment"] for row in rows),
    }


def _build_split_pair_summary(track_summaries: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for track in track_summaries:
        if track["is_split_camera_lane"]:
            groups.setdefault(track["base_name"], []).append(track)

    pairs: list[dict] = []
    for base_name, tracks in groups.items():
        lanes = _sorted_unique(track["lane"] for track in tracks)
        source_indexes = _sorted_unique(
            index
            for track in tracks
            for index in track["source_track_indexes"]
        )
        link_group_indexes = _sorted_unique(
            index
            for track in tracks
            for index in track["link_group_indexes"]
        )
        pairs.append(
            {
                "base_name": base_name,
                "lanes": lanes,
                "source_track_indexes": source_indexes,
                "link_group_indexes": link_group_indexes,
                "complete": 1 in source_indexes and 2 in source_indexes and bool(link_group_indexes),
                "track_count": len(tracks),
            }
        )
    return pairs


def _cause_hints(issues: list[dict], split_pairs: list[dict]) -> list[str]:
    hints: list[str] = []
    if any(issue["code"] == "lane_sem_link_estereo" for issue in issues):
        hints.append(
            "Os canais E/D estao separados no XML, mas nao foram declarados como par estereo via link/groupindex. "
            "Nesse caso o Premiere pode tratar cada lane como mono independente."
        )
        hints.append(
            "A correcao deve criar links de grupo entre os clipitems E/D do mesmo arquivo."
        )
    if any(not pair["complete"] for pair in split_pairs):
        hints.append("Existe camera com par E/D incompleto; isso pode indicar arquivo mono ou mapeamento incompleto no XML.")
    return hints


def _overall_status(issues: list[dict]) -> str:
    if any(issue["severity"] == "ERRO" for issue in issues):
        return "ERRO"
    if issues:
        return "ATENCAO"
    return "OK"


def _issue(severity: str, code: str, track_name: str, clip_name: str, message: str) -> dict:
    return {
        "severity": severity,
        "code": code,
        "track_name": track_name,
        "clip_name": clip_name,
        "message": message,
    }


def _parse_audio_lane(track_name: str) -> dict:
    match = re.match(r"^A\d+\s+-\s+(?P<name>.*?)(?:\s+(?P<lane>E|D|Ch\s+\d+))?$", track_name.strip())
    base_name = track_name
    lane = ""
    if match:
        base_name = match.group("name") or track_name
        lane = match.group("lane") or ""

    expected = None
    if lane == "E":
        expected = 1
    elif lane == "D":
        expected = 2
    elif lane.startswith("Ch "):
        expected = _int(lane[3:])

    kind = "camera_split_lane" if expected else "audio_track"
    return {
        "kind": kind,
        "base_name": base_name,
        "lane": lane,
        "expected_source_track_index": expected,
    }



def _clipitem_link_specs(clipitem: ET.Element) -> list[str]:
    specs: list[str] = []
    for link in clipitem.findall("link"):
        ref = _text(link.find("linkclipref"))
        media_type = _text(link.find("mediatype"))
        track_index = _text(link.find("trackindex"))
        clip_index = _text(link.find("clipindex"))
        group_index = _text(link.find("groupindex"))
        specs.append(f"{ref}:{media_type}:track{track_index}:clip{clip_index}:group{group_index}")
    return specs


def _clipitem_link_group_indexes(clipitem: ET.Element) -> list[int]:
    return _sorted_unique(
        _int(_text(link.find("groupindex")))
        for link in clipitem.findall("link")
    )

def _audio_channel_specs(audio_node: ET.Element | None) -> list[str]:
    if audio_node is None:
        return []
    specs: list[str] = []
    for channel in audio_node.findall("audiochannel"):
        label = _text(channel.find("channellabel"))
        source = _text(channel.find("sourcechannel"))
        specs.append(f"{label}:{source}")
    return specs


def _has_pan_or_output_assignment(element: ET.Element) -> bool:
    for node in element.iter():
        tag = _strip_namespace(node.tag).casefold()
        text = _text(node).casefold()
        if "outputchannel" in tag or "panner" in tag or tag == "pan":
            return True
        if tag in {"name", "parameterid"} and any(token in text for token in ("pan", "balance", "output")):
            return True
    return False


def _pathurl_to_path(pathurl: str) -> str:
    if not pathurl:
        return ""
    parsed = urlparse(pathurl)
    if parsed.scheme != "file":
        return pathurl
    raw_path = unquote(parsed.path or "")
    if re.match(r"^/[A-Za-z]:/", raw_path):
        raw_path = raw_path[1:]
    return raw_path.replace("/", "\\")


def _sorted_unique(values) -> list:
    unique = []
    for value in values:
        if value in (None, ""):
            continue
        if value not in unique:
            unique.append(value)
    return sorted(unique, key=lambda item: str(item))


def _text(element: ET.Element | None) -> str:
    return "" if element is None or element.text is None else str(element.text).strip()


def _int(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _strip_namespace(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]



