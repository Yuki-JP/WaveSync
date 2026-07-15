"""Create a project config JSON for the multicamera sync pipeline.

Examples:
  python tools/make_config.py --name casamento_soho ^
    -r "D:/evento/02 AUDIOS" ^
    -t "D:/evento/01 CAMERAS"

  python tools/make_config.py --from-selection selections/casamento_soho_selection.json

  python tools/make_config.py --name casamento_soho_teste ^
    -r "D:/evento/02 AUDIOS" --reference-filter DJI_06 DJI_07 MONO-017 ^
    -t "D:/evento/01 CAMERAS" --target-range A7IV_20260411_9715..A7IV_20260411_9729 C0020..C0029
"""

from __future__ import annotations

import argparse
import fnmatch
import glob
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_DIR = PROJECT_ROOT / "configs"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "output"

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".mts"}
AUDIO_EXTENSIONS = {".wav", ".wave", ".mp3", ".aac", ".m4a", ".flac", ".ogg", ".wma"}
TEMPORARY_SUFFIXES = {".tmp", ".temp", ".part", ".crdownload"}


@dataclass(frozen=True)
class RangeSpec:
    raw: str
    start_text: str
    end_text: str
    start_key: tuple[tuple[int, object], ...] | None = None
    end_key: tuple[tuple[int, object], ...] | None = None
    start_number: int | None = None
    end_number: int | None = None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Gera um JSON de config compativel com main.py --config."
    )
    parser.add_argument(
        "--from-selection",
        default=None,
        help=(
            "JSON pequeno com paths, filtros e ranges. "
            "Argumentos passados na CLI sobrescrevem a selecao."
        ),
    )
    parser.add_argument(
        "--name",
        default=None,
        help="Nome do preset. Ex: casamento_soho, juliana_caue_cerimonia.",
    )
    parser.add_argument(
        "-r",
        "--reference",
        "--references",
        dest="references",
        nargs="+",
        default=None,
        help="Arquivos ou pastas de audio de referencia.",
    )
    parser.add_argument(
        "-t",
        "--target",
        "--targets",
        dest="targets",
        nargs="+",
        default=None,
        help="Arquivos ou pastas de videos alvo.",
    )
    parser.add_argument(
        "--config-output",
        default=None,
        help="Caminho do JSON gerado. Padrao: configs/<name>.json.",
    )
    parser.add_argument(
        "--xml-output",
        default=None,
        help="Output XML gravado dentro do JSON. Padrao: output/<name>.xml.",
    )
    parser.add_argument(
        "--reference-filter",
        nargs="*",
        default=None,
        help="Inclui referencias por substring/glob. OR entre filtros.",
    )
    parser.add_argument(
        "--target-filter",
        nargs="*",
        default=None,
        help="Inclui targets por substring/glob. OR entre filtros.",
    )
    parser.add_argument(
        "--reference-range",
        nargs="*",
        default=None,
        help="Ranges inclusivos de referencias. Ex: DJI_06..DJI_07 ou 6-7.",
    )
    parser.add_argument(
        "--target-range",
        nargs="*",
        default=None,
        help=(
            "Ranges inclusivos de targets. "
            "Ex: A7IV_20260411_9715..A7IV_20260411_9729 ou C0020..C0029."
        ),
    )
    parser.add_argument(
        "--no-recursive",
        action="store_true",
        help="Nao varre subpastas dentro das pastas informadas.",
    )
    parser.add_argument(
        "--include-proxies",
        action="store_true",
        help="Nao ignora videos com Proxy no nome/caminho.",
    )
    parser.add_argument(
        "--include-drift-corrected",
        action="store_true",
        help="Nao ignora referencias com drift_corrected no nome/caminho.",
    )
    parser.add_argument(
        "--use-metadata",
        action="store_true",
        help="Gera ignore_metadata=false no JSON.",
    )
    parser.add_argument(
        "--no-clock-model",
        action="store_true",
        help="Gera use_camera_clock_model=false no JSON.",
    )
    parser.add_argument(
        "--camera-global-offset",
        type=float,
        default=None,
        help="Adiciona camera_global_offset ao JSON.",
    )
    parser.add_argument(
        "--camera-offset",
        action="append",
        default=None,
        metavar="CAMERA=SECONDS",
        help="Adiciona offsets por camera ao JSON. Pode repetir.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Sobrescreve config existente.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Mostra resumo e JSON, mas nao grava arquivo.",
    )
    return parser.parse_args(argv)


def load_selection(selection_path: str | None) -> tuple[dict, Path | None]:
    if not selection_path:
        return {}, None

    path = Path(selection_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"Selection JSON nao encontrado: {path}")
    if not path.is_file():
        raise ValueError(f"Selection deve ser arquivo JSON: {path}")

    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"Selection JSON deve conter um objeto: {path}")
    return payload, path.resolve()


def flatten_string_list(value: object, label: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, Path)):
        return [str(value)]
    if isinstance(value, dict):
        items: list[str] = []
        for nested_value in value.values():
            items.extend(flatten_string_list(nested_value, label))
        return items
    if isinstance(value, (list, tuple)):
        items = []
        for nested_item in value:
            items.extend(flatten_string_list(nested_item, label))
        return items
    raise ValueError(f"Valor invalido em {label}: {value!r}")


def resolve_grouped_selection_paths(
    value: object,
    label: str,
    base_dir: Path | None,
) -> dict[str, list[str]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{label} deve ser um objeto com grupos nomeados.")

    grouped: dict[str, list[str]] = {}
    for raw_group_name, raw_paths in value.items():
        group_name = str(raw_group_name).strip()
        if not group_name:
            raise ValueError(f"{label} contem um grupo sem nome.")
        paths = resolve_selection_paths(raw_paths, f"{label}.{group_name}", base_dir)
        if not paths:
            raise ValueError(f"{label}.{group_name} nao contem arquivos.")
        grouped[group_name] = paths
    return grouped


def looks_like_grouped_selection(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    path_keys = {"path", "paths", "file", "files"}
    return not any(str(key) in path_keys for key in value)


def selection_value(
    selection: dict,
    *keys: str,
    default: object = None,
) -> object:
    for key in keys:
        if key in selection:
            return selection[key]
    return default


def select_cli_or_selection(
    args: argparse.Namespace,
    attr_name: str,
    selection: dict,
    *selection_keys: str,
    default: object = None,
) -> object:
    cli_value = getattr(args, attr_name)
    if cli_value not in (None, []):
        return cli_value
    return selection_value(selection, *selection_keys, default=default)


def resolve_selection_path_text(raw_path: str, base_dir: Path | None) -> str:
    path = Path(raw_path).expanduser()
    if path.is_absolute() or base_dir is None:
        return str(path)
    return str((base_dir / path).resolve())


def resolve_selection_paths(value: object, label: str, base_dir: Path | None) -> list[str]:
    return [
        resolve_selection_path_text(raw_path, base_dir)
        for raw_path in flatten_string_list(value, label)
    ]


def bool_from_selection(value: object, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"1", "true", "yes", "sim", "on"}:
            return True
        if normalized in {"0", "false", "no", "nao", "não", "off"}:
            return False
    return bool(value)


def apply_selection(args: argparse.Namespace) -> argparse.Namespace:
    selection, selection_path = load_selection(args.from_selection)
    selection_dir = selection_path.parent if selection_path else None
    args.selection_path = str(selection_path) if selection_path else None

    args.name = select_cli_or_selection(args, "name", selection, "name")

    selected_references_value = selection_value(selection, "references", "reference")
    selected_targets_value = selection_value(selection, "targets", "target")
    explicit_reference_groups = selection_value(
        selection,
        "reference_groups",
        "referenceGroups",
        "selected_references",
        "selectedReferences",
    )
    explicit_target_groups = selection_value(
        selection,
        "target_groups",
        "targetGroups",
        "selected_targets",
        "selectedTargets",
    )
    if explicit_reference_groups is None and looks_like_grouped_selection(
        selected_references_value
    ):
        explicit_reference_groups = selected_references_value
    if explicit_target_groups is None and looks_like_grouped_selection(selected_targets_value):
        explicit_target_groups = selected_targets_value

    cli_references_supplied = args.references not in (None, [])
    cli_targets_supplied = args.targets not in (None, [])
    args.reference_groups = (
        {}
        if cli_references_supplied
        else resolve_grouped_selection_paths(
            explicit_reference_groups,
            "reference_groups",
            selection_dir,
        )
    )
    args.target_groups = (
        {}
        if cli_targets_supplied
        else resolve_grouped_selection_paths(
            explicit_target_groups,
            "target_groups",
            selection_dir,
        )
    )

    args.references = resolve_selection_paths(
        []
        if args.reference_groups
        else select_cli_or_selection(args, "references", selection, "references", "reference"),
        "references",
        selection_dir,
    )
    args.targets = resolve_selection_paths(
        []
        if args.target_groups
        else select_cli_or_selection(args, "targets", selection, "targets", "target"),
        "targets",
        selection_dir,
    )
    args.config_output = select_cli_or_selection(
        args,
        "config_output",
        selection,
        "config_output",
        "configOutput",
    )
    args.xml_output = select_cli_or_selection(
        args,
        "xml_output",
        selection,
        "xml_output",
        "xmlOutput",
        "output",
    )
    args.reference_filter = flatten_string_list(
        select_cli_or_selection(
            args,
            "reference_filter",
            selection,
            "reference_filter",
            "referenceFilter",
        ),
        "reference_filter",
    )
    args.target_filter = flatten_string_list(
        select_cli_or_selection(args, "target_filter", selection, "target_filter", "targetFilter"),
        "target_filter",
    )
    args.reference_range = flatten_string_list(
        select_cli_or_selection(
            args,
            "reference_range",
            selection,
            "reference_range",
            "referenceRange",
        ),
        "reference_range",
    )
    args.target_range = flatten_string_list(
        select_cli_or_selection(args, "target_range", selection, "target_range", "targetRange"),
        "target_range",
    )
    args.camera_offset = flatten_string_list(
        select_cli_or_selection(
            args,
            "camera_offset",
            selection,
            "camera_offset",
            "camera_offsets",
            "cameraOffsets",
        ),
        "camera_offset",
    )

    if "recursive" in selection and not args.no_recursive:
        args.no_recursive = not bool_from_selection(selection["recursive"], default=True)
    args.include_proxies = args.include_proxies or bool_from_selection(
        selection_value(selection, "include_proxies", "includeProxies"),
        default=False,
    )
    args.include_drift_corrected = args.include_drift_corrected or bool_from_selection(
        selection_value(selection, "include_drift_corrected", "includeDriftCorrected"),
        default=False,
    )

    if "ignore_metadata" in selection or "ignoreMetadata" in selection:
        ignore_metadata = bool_from_selection(
            selection_value(selection, "ignore_metadata", "ignoreMetadata"),
            default=True,
        )
        args.use_metadata = args.use_metadata or not ignore_metadata
    if "use_metadata" in selection or "useMetadata" in selection:
        args.use_metadata = args.use_metadata or bool_from_selection(
            selection_value(selection, "use_metadata", "useMetadata"),
            default=False,
        )

    if "use_camera_clock_model" in selection or "useCameraClockModel" in selection:
        use_clock_model = bool_from_selection(
            selection_value(selection, "use_camera_clock_model", "useCameraClockModel"),
            default=True,
        )
        args.no_clock_model = args.no_clock_model or not use_clock_model
    if "no_clock_model" in selection or "noClockModel" in selection:
        args.no_clock_model = args.no_clock_model or bool_from_selection(
            selection_value(selection, "no_clock_model", "noClockModel"),
            default=False,
        )

    if args.camera_global_offset is None:
        selected_offset = selection_value(
            selection,
            "camera_global_offset",
            "cameraGlobalOffset",
        )
        if selected_offset is not None:
            args.camera_global_offset = float(selected_offset)

    args.overwrite = args.overwrite or bool_from_selection(
        selection_value(selection, "overwrite"),
        default=False,
    )
    args.dry_run = args.dry_run or bool_from_selection(
        selection_value(selection, "dry_run", "dryRun"),
        default=False,
    )

    if not args.name:
        raise ValueError("Informe --name ou use --from-selection com a chave name.")
    if not args.references and not args.reference_groups:
        raise ValueError(
            "Informe -r/--reference ou use --from-selection com references/reference_groups."
        )
    if not args.targets and not args.target_groups:
        raise ValueError(
            "Informe -t/--targets ou use --from-selection com targets/target_groups."
        )

    return args


def slugify(text: str) -> str:
    text = text.strip().casefold()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text or "preset"


def natural_key(text: str) -> tuple[tuple[int, object], ...]:
    parts = re.split(r"(\d+)", text.casefold())
    return tuple((1, int(part)) if part.isdigit() else (0, part) for part in parts)


def natural_path_key(path: Path) -> tuple[tuple[int, object], ...]:
    return natural_key(path.name)


def normalize_endpoint(value: str) -> str:
    text = value.strip().strip('"').strip("'")
    suffix = Path(text).suffix.casefold()
    if suffix in VIDEO_EXTENSIONS or suffix in AUDIO_EXTENSIONS:
        return Path(text).stem
    return text


def parse_range_specs(raw_specs: Iterable[str]) -> list[RangeSpec]:
    specs: list[RangeSpec] = []
    for raw in raw_specs:
        text = raw.strip()
        if not text:
            continue

        numeric_match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", text)
        if numeric_match:
            start_number = int(numeric_match.group(1))
            end_number = int(numeric_match.group(2))
            if start_number > end_number:
                start_number, end_number = end_number, start_number
            specs.append(
                RangeSpec(
                    raw=raw,
                    start_text=numeric_match.group(1),
                    end_text=numeric_match.group(2),
                    start_number=start_number,
                    end_number=end_number,
                )
            )
            continue

        if ".." in text:
            start_text, end_text = text.split("..", 1)
        elif "--" in text:
            start_text, end_text = text.split("--", 1)
        elif "-" in text:
            start_text, end_text = text.split("-", 1)
        else:
            raise ValueError(
                f"Range invalido: {raw}. Use START..END, START--END ou 123-456."
            )

        start = normalize_endpoint(start_text)
        end = normalize_endpoint(end_text)
        start_key = natural_key(start)
        end_key = natural_key(end)
        if start_key > end_key:
            start_key, end_key = end_key, start_key
            start, end = end, start
        specs.append(
            RangeSpec(
                raw=raw,
                start_text=start,
                end_text=end,
                start_key=start_key,
                end_key=end_key,
            )
        )
    return specs


def has_glob_magic(value: str) -> bool:
    return any(char in value for char in "*?[]")


def expand_inputs(raw_paths: list[str]) -> list[Path]:
    paths: list[Path] = []
    for raw in raw_paths:
        if has_glob_magic(raw):
            matches = [Path(match) for match in glob.glob(raw, recursive=True)]
            if not matches:
                raise FileNotFoundError(f"Nenhum caminho encontrou o glob: {raw}")
            paths.extend(matches)
            continue
        paths.append(Path(raw).expanduser())
    return paths


def is_ignored_path(path: Path) -> bool:
    for part in path.parts:
        if part.startswith("."):
            return True

    name = path.name
    lower_name = name.casefold()
    try:
        is_windows_hidden = bool(getattr(path.stat(), "st_file_attributes", 0) & 0x2)
    except FileNotFoundError:
        is_windows_hidden = False
    return (
        is_windows_hidden
        or name.startswith("~")
        or lower_name.endswith(".tmp")
        or path.suffix.casefold() in TEMPORARY_SUFFIXES
    )


def is_proxy_file(path: Path) -> bool:
    text = " ".join(path.parts).casefold()
    return "proxy" in text


def is_drift_corrected_file(path: Path) -> bool:
    text = " ".join(path.parts).casefold()
    return "drift_corrected" in text or "pluraleyes_drift_corrected" in text


def matches_filters(path: Path, filters: list[str]) -> bool:
    if not filters:
        return True

    candidates = [
        path.name.casefold(),
        path.stem.casefold(),
        str(path).replace("\\", "/").casefold(),
    ]
    for raw_filter in filters:
        pattern = raw_filter.replace("\\", "/").casefold()
        for candidate in candidates:
            if pattern in candidate:
                return True
            if fnmatch.fnmatch(candidate, pattern):
                return True
    return False


def extract_numbers(path: Path) -> list[int]:
    return [int(value) for value in re.findall(r"\d+", path.stem)]


def matches_range(path: Path, range_spec: RangeSpec) -> bool:
    if range_spec.start_number is not None and range_spec.end_number is not None:
        return any(
            range_spec.start_number <= number <= range_spec.end_number
            for number in extract_numbers(path)
        )

    if range_spec.start_key is None or range_spec.end_key is None:
        return False

    key = natural_key(path.stem)
    return range_spec.start_key <= key <= range_spec.end_key


def matches_ranges(path: Path, ranges: list[RangeSpec]) -> bool:
    if not ranges:
        return True
    return any(matches_range(path, range_spec) for range_spec in ranges)


def scan_media(
    inputs: list[str],
    *,
    extensions: set[str],
    filters: list[str],
    ranges: list[RangeSpec],
    recursive: bool,
    include_proxies: bool = False,
    include_drift_corrected: bool = False,
) -> list[Path]:
    files: list[Path] = []
    for input_path in expand_inputs(inputs):
        path = input_path.expanduser()
        if not path.exists():
            raise FileNotFoundError(f"Caminho nao encontrado: {path}")

        candidates: list[Path]
        if path.is_file():
            candidates = [path]
        elif path.is_dir():
            iterator = path.rglob("*") if recursive else path.glob("*")
            candidates = [candidate for candidate in iterator if candidate.is_file()]
        else:
            raise ValueError(f"Caminho deve ser arquivo ou pasta: {path}")

        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved.suffix.casefold() not in extensions:
                continue
            if is_ignored_path(resolved):
                continue
            if not include_proxies and extensions == VIDEO_EXTENSIONS and is_proxy_file(resolved):
                continue
            if (
                not include_drift_corrected
                and extensions == AUDIO_EXTENSIONS
                and is_drift_corrected_file(resolved)
            ):
                continue
            if not matches_filters(resolved, filters):
                continue
            if not matches_ranges(resolved, ranges):
                continue
            files.append(resolved)

    return sorted(set(files), key=lambda item: (natural_path_key(item), str(item).casefold()))


def unique_slug(raw_name: str, used: set[str]) -> str:
    base = slugify(raw_name)
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def build_exact_media_groups(
    groups: dict[str, list[str]],
    *,
    extensions: set[str],
    include_proxies: bool = False,
    include_drift_corrected: bool = False,
) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    used_group_names: set[str] = set()
    seen_paths: dict[Path, str] = {}

    for raw_group_name, raw_paths in groups.items():
        group_name = unique_slug(raw_group_name, used_group_names)
        selected_paths: list[str] = []

        for raw_path in raw_paths:
            for path in expand_inputs([raw_path]):
                resolved = path.expanduser().resolve()
                if not resolved.exists():
                    raise FileNotFoundError(f"Arquivo selecionado nao encontrado: {resolved}")
                if not resolved.is_file():
                    raise ValueError(
                        f"Selecao explicita aceita apenas arquivos, nao pastas: {resolved}"
                    )
                if resolved.suffix.casefold() not in extensions:
                    supported = ", ".join(sorted(extensions))
                    raise ValueError(
                        f"Arquivo selecionado tem extensao invalida: {resolved} "
                        f"(suportadas: {supported})"
                    )
                if is_ignored_path(resolved):
                    raise ValueError(f"Arquivo selecionado esta marcado como ignorado: {resolved}")
                if not include_proxies and extensions == VIDEO_EXTENSIONS and is_proxy_file(resolved):
                    raise ValueError(f"Proxy selecionado por engano: {resolved}")
                if (
                    not include_drift_corrected
                    and extensions == AUDIO_EXTENSIONS
                    and is_drift_corrected_file(resolved)
                ):
                    raise ValueError(f"Referencia drift_corrected selecionada por engano: {resolved}")
                if resolved in seen_paths:
                    raise ValueError(
                        "Arquivo selecionado em mais de um grupo: "
                        f"{resolved} ({seen_paths[resolved]} e {group_name})"
                    )

                seen_paths[resolved] = group_name
                selected_paths.append(path_for_json(resolved))

        if not selected_paths:
            raise ValueError(f"Grupo sem arquivos validos: {raw_group_name}")
        grouped[group_name] = selected_paths

    return grouped


def group_by_parent(files: list[Path]) -> dict[str, list[str]]:
    grouped_paths: dict[str, list[Path]] = {}
    for path in files:
        key = slugify(path.parent.name or "media")
        grouped_paths.setdefault(key, []).append(path)

    grouped: dict[str, list[str]] = {}
    for key in sorted(grouped_paths, key=natural_key):
        grouped[key] = [
            path_for_json(path)
            for path in sorted(
                grouped_paths[key],
                key=lambda item: (natural_path_key(item), str(item).casefold()),
            )
        ]
    return grouped


def path_for_json(path: Path) -> str:
    return path.resolve().as_posix()


def count_grouped(grouped: dict[str, list[str]]) -> int:
    return sum(len(items) for items in grouped.values())


def build_config(args: argparse.Namespace) -> tuple[dict, Path]:
    preset_name = slugify(args.name)
    config_output = (
        Path(args.config_output).expanduser()
        if args.config_output
        else DEFAULT_CONFIG_DIR / f"{preset_name}.json"
    )
    xml_output = (
        Path(args.xml_output).expanduser().as_posix()
        if args.xml_output
        else f"output/{preset_name}.xml"
    )

    reference_ranges = parse_range_specs(args.reference_range)
    target_ranges = parse_range_specs(args.target_range)

    if args.reference_groups:
        references = build_exact_media_groups(
            args.reference_groups,
            extensions=AUDIO_EXTENSIONS,
            include_drift_corrected=args.include_drift_corrected,
        )
    else:
        references = group_by_parent(
            scan_media(
                args.references,
                extensions=AUDIO_EXTENSIONS,
                filters=args.reference_filter,
                ranges=reference_ranges,
                recursive=not args.no_recursive,
                include_drift_corrected=args.include_drift_corrected,
            )
        )

    if args.target_groups:
        targets = build_exact_media_groups(
            args.target_groups,
            extensions=VIDEO_EXTENSIONS,
            include_proxies=args.include_proxies,
        )
    else:
        targets = group_by_parent(
            scan_media(
                args.targets,
                extensions=VIDEO_EXTENSIONS,
                filters=args.target_filter,
                ranges=target_ranges,
                recursive=not args.no_recursive,
                include_proxies=args.include_proxies,
            )
        )

    if not references:
        raise FileNotFoundError("Nenhuma referencia de audio encontrada com os criterios.")
    if not targets:
        raise FileNotFoundError("Nenhum target de video encontrado com os criterios.")

    config: dict[str, object] = {
        "ignore_metadata": not args.use_metadata,
        "use_camera_clock_model": not args.no_clock_model,
        "output": xml_output,
        "references": references,
        "targets": targets,
    }
    if args.camera_global_offset is not None:
        config["camera_global_offset"] = args.camera_global_offset
    if args.camera_offset:
        config["camera_offset"] = list(args.camera_offset)

    return config, config_output


def print_summary(config: dict, config_output: Path) -> None:
    references = config.get("references") or {}
    targets = config.get("targets") or {}
    if not isinstance(references, dict) or not isinstance(targets, dict):
        return

    print("")
    print("=" * 72)
    print("CONFIG GERADO")
    print("=" * 72)
    print(f"Arquivo config : {config_output}")
    print(f"Output XML     : {config.get('output')}")
    print(f"Referencias    : {count_grouped(references)} arquivo(s) em {len(references)} grupo(s)")
    for group_name, items in references.items():
        print(f"  - {group_name}: {len(items)}")
    print(f"Targets        : {count_grouped(targets)} arquivo(s) em {len(targets)} grupo(s)")
    for group_name, items in targets.items():
        print(f"  - {group_name}: {len(items)}")
    print(f"Ignore metadata: {config.get('ignore_metadata')}")
    print(f"Clock model    : {config.get('use_camera_clock_model')}")
    print("=" * 72)


def write_config(config: dict, config_output: Path, *, overwrite: bool) -> None:
    if config_output.exists() and not overwrite:
        raise FileExistsError(
            f"Config ja existe: {config_output}. Use --overwrite para substituir."
        )
    config_output.parent.mkdir(parents=True, exist_ok=True)
    config_output.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    try:
        args = apply_selection(parse_args(argv))
        config, config_output = build_config(args)
        print_summary(config, config_output)
        if args.dry_run:
            print("")
            print(json.dumps(config, ensure_ascii=False, indent=2))
            return 0

        write_config(config, config_output, overwrite=args.overwrite)
        print("")
        print(f"[OK] Config salvo: {config_output}")
        print("")
        print("Proximo comando:")
        print(f'  python main.py --config "{config_output}"')
        return 0
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
