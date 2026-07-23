"""
CLI principal do sincronizador multicamera.

Responsabilidades deste modulo:
- resolver entradas de referencias e targets;
- preparar features de audio;
- alinhar referencias numa regua master;
- calcular offsets de cameras com DSP, modelo de relogio e refinamento local;
- aplicar continuidade de arquivos quando for seguro;
- entregar resultados para XML e auditoria.

Relatorios CSV/JSON, resumo final e TrackCheck ficam em backend.audit_report.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import logging
import os
import re
import shutil
import sys
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

import numpy as np

from backend.audit_report import (
    build_camera_track_check,
    build_summary,
    build_sync_audit_rows,
    log_camera_track_check,
    write_sync_audit_reports,
)
from backend.audio_processor import prepare_cached_audio_features


def application_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


PROJECT_ROOT = application_root()
DEFAULT_OUTPUT_XML = PROJECT_ROOT / "output" / "timeline_sincronizada.xml"
TEMP_DIR = PROJECT_ROOT / "temp"
CACHE_ROOT_DIR = TEMP_DIR / "cache"
AUDIO_CACHE_DIR = TEMP_DIR / "cache" / "audio"
CACHE_CLEANUP_STATE_PATH = TEMP_DIR / "cache_cleanup_state.json"
CACHE_CLEANUP_INTERVAL_SYNCS = 1

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".mts"}
AUDIO_EXTENSIONS = {".wav", ".wave", ".mp3", ".aac", ".m4a", ".flac", ".ogg", ".wma"}
TEMPORARY_SUFFIXES = {".tmp", ".temp", ".part", ".crdownload"}

HYBRID_WINDOW_SECONDS = 300.0
IGNORE_METADATA_ASSIST_WINDOW_SECONDS = 300.0
IGNORE_METADATA_ASSIST_MAX_FULL_SCAN_DEVIATION_SECONDS = 300.0
METADATA_ANCHOR_MIN_REFERENCE_OVERLAP_SECONDS = 10.0
MIN_CONFIDENCE_Z_SCORE = 8.0
MIN_CONFIDENCE_PROMINENCE = 1.15
MIN_FULL_SCAN_Z_SCORE = 4.0
MAX_TIME_GAP_SECONDS = 7_200.0
MIN_REFERENCE_ALIGNMENT_Z_SCORE = 5.0
MIN_REFERENCE_OVERLAP_SECONDS = 30.0
MAX_REFERENCE_ALIGNMENT_DEVIATION_SECONDS = 7_200.0
REFERENCE_CONTINUITY_TOLERANCE_SECONDS = 15.0
MIN_CAMERA_ANCHOR_DURATION_SECONDS = 60.0
MIN_INDIVIDUAL_DSP_DURATION_SECONDS = 30.0
CAMERA_BASE_CONSENSUS_TOLERANCE_SECONDS = 12.0
CAMERA_ORDER_OVERLAP_TOLERANCE_SECONDS = 2.0
CAMERA_CLOCK_MIN_POINTS = 2
CAMERA_CLOCK_MIN_SEPARATION_SECONDS = 120.0
CAMERA_CLOCK_INLIER_TOLERANCE_SECONDS = 1.5
CAMERA_CLOCK_MAX_ABS_DRIFT_PPM = 5_000.0
CAMERA_LOCAL_REFINE_WINDOW_SECONDS = 3.0
CAMERA_LOCAL_REFINE_MAX_DELTA_SECONDS = 1.5
CAMERA_LOCAL_REFINE_MIN_Z_SCORE = 4.0
CAMERA_LOCAL_REFINE_WEAK_MAX_DELTA_SECONDS = 0.5
CAMERA_LOCAL_REFINE_WEAK_MIN_Z_SCORE = 1.15
CAMERA_LOCAL_REFINE_WEAK_MIN_PROMINENCE = 2.0
CAMERA_LOCAL_REFINE_MEDIUM_MAX_DELTA_SECONDS = 1.0
CAMERA_LOCAL_REFINE_MEDIUM_MIN_Z_SCORE = 2.5
CAMERA_LOCAL_REFINE_MEDIUM_MIN_PROMINENCE = 2.0
CAMERA_WEAK_INDIVIDUAL_MAX_DELTA_SECONDS = 1.0
CAMERA_WEAK_INDIVIDUAL_MIN_Z_SCORE = 2.5
CAMERA_WEAK_INDIVIDUAL_MIN_PROMINENCE = 1.2
CAMERA_LOCAL_REFINE_STRONG_Z_SCORE = 8.0
CAMERA_LOCAL_REFINE_STRONG_PROMINENCE = 2.0
CAMERA_POST_CUT_NATIVE_LATE_THRESHOLD_SECONDS = 0.35
CAMERA_POST_CUT_NATIVE_MIN_PREVIOUS_DURATION_SECONDS = 120.0
CAMERA_POST_CUT_NATIVE_MAX_GAP_SECONDS = 3.0
CAMERA_POST_CUT_LOCAL_MIN_PROMINENCE = 1.2
CAMERA_SHORT_CLIP_NATIVE_MAX_DURATION_SECONDS = 20.0
CAMERA_SHORT_CLIP_NATIVE_MAX_GAP_SECONDS = 60.0
CAMERA_SHORT_CLIP_NATIVE_MAX_DELTA_SECONDS = 2.0
CAMERA_PEER_REFINE_WINDOW_SECONDS = 8.0
CAMERA_PEER_REFINE_MAX_DELTA_SECONDS = 0.8
CAMERA_PEER_REFINE_MIN_Z_SCORE = 2.5
CAMERA_PEER_REFINE_MIN_PROMINENCE = 1.2
CAMERA_PEER_REFINE_MIN_OVERLAP_SECONDS = 10.0
CAMERA_PEER_GAP_BACKFILL_TOLERANCE_SECONDS = 0.15
CAMERA_PEER_GAP_DIRECT_TRIM_WINDOW_SECONDS = 3.0
CAMERA_PEER_GAP_DIRECT_TRIM_MAX_DELTA_SECONDS = 0.4
CAMERA_PEER_GAP_DIRECT_TRIM_MIN_Z_SCORE = 0.75
CAMERA_PEER_GAP_DIRECT_TRIM_MIN_PROMINENCE = 0.5
CAMERA_PEER_BRIDGE_MIN_Z_SCORE = 8.0
CAMERA_PEER_BRIDGE_MIN_PROMINENCE = 2.0
CAMERA_PEER_BRIDGE_MIN_OVERLAP_SECONDS = 10.0
CAMERA_PEER_BLOCK_BRIDGE_MIN_Z_SCORE = 8.0
CAMERA_PEER_BLOCK_BRIDGE_MIN_PROMINENCE = 2.0
CAMERA_PEER_BLOCK_BRIDGE_MIN_OVERLAP_SECONDS = 10.0
CAMERA_PEER_BLOCK_BRIDGE_BASE_TOLERANCE_SECONDS = 3.0
CAMERA_PEER_BLOCK_BRIDGE_MIN_METADATA_IMPROVEMENT_SECONDS = 30.0
INVISIBLE_ANCHOR_MAX_EXTRA_CLIPS_PER_CAMERA = 8
INVISIBLE_ANCHOR_FORWARD_RADIUS = 10
INVISIBLE_ANCHOR_BACKWARD_RADIUS = 3
INVISIBLE_ANCHOR_NEAR_FORWARD_COUNT = 6
INVISIBLE_ANCHOR_LONG_FORWARD_COUNT = 2
INVISIBLE_ANCHOR_BACKWARD_COUNT = 2
FILE_SPANNING_TOLERANCE_SECONDS = 3.0
FILE_SPANNING_MIN_PREVIOUS_DURATION_SECONDS = 300.0
FILE_SPANNING_LOW_Z_SCORE_THRESHOLD = 6.0
EPSILON = 1e-12

logger = logging.getLogger("wavesync.pipeline")


@dataclass(frozen=True)
class CorrelationResult:
    offset_seconds: float
    peak_value: float
    z_score: float
    prominence_ratio: float
    low_confidence: bool
    source: str


@dataclass(frozen=True)
class PreparedClip:
    path: Path
    key: str
    wav_path: Path
    duration_seconds: float
    features: object
    estimated_start_time: float | None
    estimated_offset_seconds: float | None
    camera_name: str
    original_index: int
    cache_hit_wav: bool = False
    cache_hit_features: bool = False
    is_invisible_anchor: bool = False


@dataclass(frozen=True)
class PreparedReference:
    path: Path
    key: str
    name: str
    track_name: str
    wav_path: Path
    duration_seconds: float
    absolute_start_time: float
    timeline_offset_seconds: float
    features: object
    original_index: int
    cache_hit_wav: bool = False
    cache_hit_features: bool = False


@dataclass(frozen=True)
class ClipSyncMatch:
    clip: PreparedClip
    chosen_reference: PreparedReference
    chosen_result: CorrelationResult
    window_result: CorrelationResult | None
    target_features: object
    metadata_ignored: bool


@dataclass(frozen=True)
class ReferenceAlignmentEdge:
    source_key: str
    target_key: str
    delta_seconds: float
    priority: int
    score: float
    method: str


@dataclass(frozen=True)
class CameraBlockPlacement:
    match: ClipSyncMatch
    anchor_match: ClipSyncMatch
    final_offset_seconds: float
    individual_dsp_offset_seconds: float
    anchor_dsp_offset_seconds: float
    camera_block_base_seconds: float
    camera_base_candidate_seconds: float
    camera_base_deviation_seconds: float
    camera_native_predicted_offset_seconds: float
    camera_clock_model_offset_seconds: float | None
    camera_clock_residual_seconds: float | None
    camera_clock_base_seconds: float | None
    camera_clock_drift_rate: float | None
    camera_clock_drift_ppm: float | None
    camera_clock_inlier_count: int
    camera_clock_candidate_count: int
    camera_clock_model_method: str
    local_refinement: LocalCameraRefinement | None
    peer_refinement: PeerCameraRefinement | None
    offset_decision_reason: str
    reference_delta_to_master_seconds: float
    native_relative_start_seconds: float
    anchor_native_relative_start_seconds: float
    native_gap_from_previous_seconds: float | None
    method: str


@dataclass(frozen=True)
class CameraAnchorCandidate:
    match: ClipSyncMatch
    individual_offset_seconds: float
    native_relative_start_seconds: float
    base_offset_seconds: float
    weight: float
    eligible_as_anchor: bool


@dataclass(frozen=True)
class CameraClockModel:
    base_offset_seconds: float
    drift_rate: float
    inlier_count: int
    candidate_count: int
    max_abs_residual_seconds: float | None
    median_abs_residual_seconds: float | None
    method: str


@dataclass(frozen=True)
class LocalCameraRefinement:
    reference: PreparedReference
    result: CorrelationResult
    final_offset_seconds: float
    delta_from_prediction_seconds: float


@dataclass(frozen=True)
class PeerCameraRefinement:
    reference_clip_name: str
    reference_camera_name: str
    result: CorrelationResult
    final_offset_seconds: float
    delta_from_current_seconds: float
    overlap_seconds: float


@dataclass(frozen=True)
class CameraBlockBridgeCandidate:
    placement: CameraBlockPlacement
    peer: CameraBlockPlacement
    result: CorrelationResult
    final_offset_seconds: float
    base_offset_seconds: float
    overlap_seconds: float


class ConsoleFormatter(logging.Formatter):
    """Formatter enxuto com marcadores visuais e cores ANSI quando disponiveis."""

    RESET = "\033[0m"
    COLORS = {
        logging.DEBUG: "\033[36m",
        logging.INFO: "\033[32m",
        logging.WARNING: "\033[33m",
        logging.ERROR: "\033[31m",
        logging.CRITICAL: "\033[35m",
    }
    MARKERS = {
        logging.DEBUG: "DEBUG",
        logging.INFO: "INFO ",
        logging.WARNING: "WARN ",
        logging.ERROR: "ERROR",
        logging.CRITICAL: "FATAL",
    }

    def __init__(self, use_color: bool) -> None:
        super().__init__("%(message)s")
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        marker = self.MARKERS.get(record.levelno, record.levelname)
        prefix = f"[{marker}]"
        if self.use_color:
            color = self.COLORS.get(record.levelno, "")
            prefix = f"{color}{prefix}{self.RESET}"
        return f"{prefix} {record.getMessage()}"


def configure_logging(verbose: bool = False) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(ConsoleFormatter(use_color=sys.stderr.isatty()))

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.DEBUG if verbose else logging.INFO)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python main.py",
        description="Sincroniza midias por audio e gera um XML FCP 7 para edicao.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help=(
            "Arquivo JSON de projeto com referencias, targets e opcoes. "
            "Argumentos passados na CLI sobrescrevem o JSON."
        ),
    )
    parser.add_argument(
        "-r",
        "--reference",
        default=None,
        nargs="+",
        help="Um ou mais arquivos/pastas de lapela.",
    )
    parser.add_argument(
        "-t",
        "--targets",
        default=None,
        nargs="+",
        help="Um ou mais arquivos/pastas de video alvo.",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help=f"Caminho de saida do XML. Padrao: {DEFAULT_OUTPUT_XML}",
    )
    parser.add_argument(
        "--audit-output",
        default=None,
        help=(
            "Prefixo ou caminho para relatorios de auditoria CSV/JSON. "
            "Padrao: mesmo nome do XML com sufixo _audit."
        ),
    )
    parser.add_argument(
        "--fps",
        default=None,
        help="Mantido por compatibilidade; o XML de Premiere e forcado para 30 NTSC.",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        default=None,
        help="Remove WAVs temporarios apos gerar o XML com sucesso.",
    )
    parser.add_argument(
        "-i",
        "--ignore-metadata",
        action="store_true",
        default=None,
        help="Ignora mtime/metadados e executa full scan de correlacao no audio inteiro.",
    )
    parser.add_argument(
        "--reference-filter",
        nargs="*",
        default=None,
        help="Filtra referencias dentro de pastas por substring ou glob. Ex: Lapela_01 *.WAV",
    )
    parser.add_argument(
        "--target-filter",
        nargs="*",
        default=None,
        help="Filtra videos dentro de pastas por substring ou glob. Ex: A7IV ZVE10 C0012.MP4",
    )
    parser.add_argument(
        "--camera-global-offset",
        type=float,
        default=None,
        help=(
            "Desloca todos os blocos de camera em segundos apos a sincronizacao. "
            "Use valor negativo para adiantar cameras atrasadas em relacao as lapelas."
        ),
    )
    parser.add_argument(
        "--camera-offset",
        action="append",
        default=None,
        metavar="CAMERA=SECONDS",
        help=(
            "Desloca uma camera especifica por substring do nome. "
            "Ex: --camera-offset A7IV=-0.3 --camera-offset ZVE10=0.1"
        ),
    )
    parser.add_argument(
        "--use-camera-clock-model",
        action="store_true",
        default=None,
        help=(
            "Usa o modelo linear de relogio por camera com refino DSP local curto, "
            "em vez de aceitar diretamente cada pico de Full Scan individual."
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=None,
        help="Exibe logs de depuracao.",
    )
    return parser.parse_args(argv)


def load_project_config(config_path: str | None) -> tuple[dict, Path | None]:
    if not config_path:
        return {}, None

    path = Path(config_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"Config de projeto nao encontrada: {path}")
    if not path.is_file():
        raise ValueError(f"Config de projeto deve ser um arquivo JSON: {path}")

    with path.open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    if not isinstance(config, dict):
        raise ValueError(f"Config de projeto deve conter um objeto JSON: {path}")
    return config, path.resolve()


def normalize_config_path_list(value: object, label: str) -> list[str]:
    """Aceita lista simples ou grupos em dict e devolve uma lista plana de paths."""
    if value is None:
        return []

    paths: list[str] = []

    def visit(item: object) -> None:
        if item is None:
            return
        if isinstance(item, (str, os.PathLike)):
            paths.append(str(item))
            return
        if isinstance(item, dict):
            known_path_keys = ("path", "paths", "file", "files")
            if any(key in item for key in known_path_keys):
                for key in known_path_keys:
                    if key in item:
                        visit(item[key])
                return
            for nested_value in item.values():
                visit(nested_value)
            return
        if isinstance(item, (list, tuple)):
            for nested_item in item:
                visit(nested_item)
            return
        raise ValueError(f"Valor invalido em {label}: {item!r}")

    visit(value)
    return paths


def normalize_string_list(value: object, label: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if item is not None]
    raise ValueError(f"Valor invalido em {label}: {value!r}")


def normalize_camera_offset_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        return [f"{camera}={seconds}" for camera, seconds in value.items()]
    return normalize_string_list(value, "camera_offset")


def config_value(
    args: argparse.Namespace,
    config: dict,
    attr_name: str,
    *config_keys: str,
    default: object = None,
) -> object:
    cli_value = getattr(args, attr_name, None)
    if cli_value is not None:
        return cli_value
    for key in config_keys:
        if key in config:
            return config[key]
    return default


def apply_project_config(args: argparse.Namespace) -> argparse.Namespace:
    config, config_path = load_project_config(args.config)
    args.project_config_path = str(config_path) if config_path else None

    args.reference = normalize_config_path_list(
        config_value(args, config, "reference", "reference", "references"),
        "reference",
    )
    args.targets = normalize_config_path_list(
        config_value(args, config, "targets", "targets"),
        "targets",
    )
    args.output = str(
        config_value(args, config, "output", "output", default=str(DEFAULT_OUTPUT_XML))
    )
    args.audit_output = config_value(
        args,
        config,
        "audit_output",
        "audit_output",
        "auditOutput",
        default=None,
    )
    args.fps = str(config_value(args, config, "fps", "fps", default="30"))
    args.cleanup = bool(config_value(args, config, "cleanup", "cleanup", default=False))
    args.ignore_metadata = bool(
        config_value(
            args,
            config,
            "ignore_metadata",
            "ignore_metadata",
            "ignoreMetadata",
            default=False,
        )
    )
    args.reference_filter = normalize_string_list(
        config_value(
            args,
            config,
            "reference_filter",
            "reference_filter",
            "referenceFilter",
        ),
        "reference_filter",
    )
    args.target_filter = normalize_string_list(
        config_value(args, config, "target_filter", "target_filter", "targetFilter"),
        "target_filter",
    )
    args.camera_global_offset = float(
        config_value(
            args,
            config,
            "camera_global_offset",
            "camera_global_offset",
            "cameraGlobalOffset",
            default=0.0,
        )
    )
    args.camera_offset = normalize_camera_offset_list(
        config_value(
            args,
            config,
            "camera_offset",
            "camera_offset",
            "camera_offsets",
            "cameraOffsets",
        )
    )
    args.use_camera_clock_model = bool(
        config_value(
            args,
            config,
            "use_camera_clock_model",
            "use_camera_clock_model",
            "useCameraClockModel",
            default=False,
        )
    )
    args.explicit_selection = bool(
        config_value(
            args,
            config,
            "explicit_selection",
            "explicit_selection",
            "explicitSelection",
            "selected_files_only",
            "selectedFilesOnly",
            default=False,
        )
    )
    args.verbose = bool(config_value(args, config, "verbose", "verbose", default=False))

    if not args.reference:
        raise ValueError("Informe -r/--reference ou use --config com a chave references.")
    if not args.targets:
        raise ValueError("Informe -t/--targets ou use --config com a chave targets.")

    return args


def resolve_existing_path(raw_path: str, label: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"{label} nao encontrado: {path}")
    return path.resolve()


def resolve_existing_paths(raw_paths: list[str], label: str) -> list[Path]:
    return [resolve_existing_path(raw_path, label) for raw_path in raw_paths]


def resolve_media_files(
    input_paths: list[Path],
    extensions: set[str],
    label: str,
    include_filters: list[str] | None = None,
) -> list[Path]:
    """Resolve arquivos diretos e pastas, com filtro opcional por substring/glob."""
    filters = [item for item in (include_filters or []) if item]
    files: list[Path] = []

    for input_path in input_paths:
        if input_path.is_file():
            if input_path.suffix.lower() not in extensions:
                raise ValueError(f"{label} com extensao nao suportada: {input_path}")
            if extensions == VIDEO_EXTENSIONS and is_proxy_file(input_path):
                logger.info("Ignorando proxy de video: %s", input_path)
                continue
            if not is_ignored_path(input_path) and path_matches_filters(input_path, filters):
                files.append(input_path.resolve())
            continue

        if not input_path.is_dir():
            raise ValueError(f"{label} deve ser arquivo ou pasta: {input_path}")

        for candidate in scan_files(input_path, extensions):
            if extensions == VIDEO_EXTENSIONS and is_proxy_file(candidate):
                logger.info("Ignorando proxy de video: %s", candidate)
                continue
            if path_matches_filters(candidate, filters):
                files.append(candidate.resolve())

    unique_files = sorted(set(files), key=lambda item: str(item).casefold())
    if not unique_files:
        filter_text = f" com filtros {filters}" if filters else ""
        raise FileNotFoundError(f"Nenhum arquivo de {label} encontrado{filter_text}.")
    return unique_files


def is_proxy_file(path: Path) -> bool:
    return "proxy" in path.name.casefold()


def deduplicate_reference_files(reference_files: list[Path]) -> list[Path]:
    """
    Mantem apenas referencias mestre, removendo versoes corrigidas de drift.

    Arquivos com `drift_corrected` no caminho ou com `_drift_corrected` no nome
    nao entram nem como tracks fisicas nem como candidatas do Full Scan.
    """
    grouped: dict[str, list[Path]] = {}
    for reference_file in reference_files:
        key = reference_dedupe_key(reference_file)
        grouped.setdefault(key, []).append(reference_file)

    deduped: list[Path] = []
    for key, files in grouped.items():
        clean_files = [path for path in files if not is_drift_corrected_reference(path)]
        if not clean_files:
            logger.warning(
                "Descartando grupo de referencia sem arquivo mestre limpo: %s",
                key,
            )
            continue

        chosen = sorted(clean_files, key=lambda path: str(path).casefold())[0]
        discarded = [path for path in files if path != chosen]
        for path in discarded:
            logger.info("Referencia duplicada/corrigida ignorada: %s", path)
        deduped.append(chosen)

    if not deduped:
        raise ValueError("Nenhuma referencia mestre limpa apos remover drift_corrected.")

    return sorted(deduped, key=lambda path: str(path).casefold())


def is_drift_corrected_reference(path: Path) -> bool:
    text = str(path).replace("\\", "/").casefold()
    return "_drift_corrected" in path.stem.casefold() or "drift_corrected" in text


def reference_dedupe_key(path: Path) -> str:
    stem = re.sub(r"_drift_corrected$", "", path.stem, flags=re.IGNORECASE)
    stem = re.sub(r"\s+", " ", stem).strip().casefold()
    return stem


def path_matches_filters(path: Path, include_filters: list[str]) -> bool:
    if not include_filters:
        return True

    name = path.name.casefold()
    full_path = str(path).replace("\\", "/").casefold()
    for raw_filter in include_filters:
        pattern = raw_filter.replace("\\", "/").casefold()
        if pattern in name or pattern in full_path:
            return True
        if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(full_path, pattern):
            return True
    return False


def resolve_reference_file(reference_path: Path) -> Path:
    if reference_path.is_file():
        if is_ignored_path(reference_path):
            raise ValueError(f"Arquivo de referencia ignorado: {reference_path}")
        return reference_path

    if not reference_path.is_dir():
        raise ValueError(f"Referencia deve ser arquivo ou pasta: {reference_path}")

    candidates = scan_files(reference_path, AUDIO_EXTENSIONS)
    if not candidates:
        raise FileNotFoundError(f"Nenhum audio de referencia encontrado em: {reference_path}")
    if len(candidates) > 1:
        formatted = "\n".join(f"  - {candidate}" for candidate in candidates[:10])
        extra = "" if len(candidates) <= 10 else f"\n  ... e mais {len(candidates) - 10}"
        raise ValueError(
            "A pasta de referencia contem mais de um audio. Informe o arquivo exato:\n"
            f"{formatted}{extra}"
        )
    return candidates[0]


def resolve_target_files(targets_path: Path) -> list[Path]:
    if targets_path.is_file():
        if targets_path.suffix.lower() not in VIDEO_EXTENSIONS:
            raise ValueError(f"Arquivo alvo nao tem extensao de video suportada: {targets_path}")
        if is_ignored_path(targets_path):
            raise ValueError(f"Arquivo alvo ignorado: {targets_path}")
        return [targets_path]

    if not targets_path.is_dir():
        raise ValueError(f"Targets deve ser pasta ou arquivo de video: {targets_path}")

    videos = scan_files(targets_path, VIDEO_EXTENSIONS)
    if not videos:
        supported = ", ".join(sorted(VIDEO_EXTENSIONS))
        raise FileNotFoundError(
            f"Nenhum video suportado encontrado em {targets_path}. Extensoes: {supported}"
        )
    return sort_targets_chronologically(videos)


def sort_targets_chronologically(target_files: list[Path]) -> list[Path]:
    """Ordena por mtime e usa nome/caminho como desempate deterministico."""
    return sorted(
        target_files,
        key=lambda path: (
            path.stat().st_mtime,
            path.name.casefold(),
            str(path).casefold(),
        ),
    )


def sort_targets_for_batch(
    target_files: list[Path],
    camera_map: dict[str, str],
    *,
    ignore_metadata: bool,
) -> list[Path]:
    """
    Ordena o lote por camera e cronologia interna.

    Com `--ignore-metadata`, o mtime nao serve nem para estimar offset nem para
    ordenar clipes dentro da camera. Nesse caso usamos ordem natural do nome.
    """
    camera_order: dict[str, int] = {}
    for target_file in target_files:
        camera_name = camera_map.get(str(target_file)) or target_file.parent.name or "CAM 01"
        camera_order.setdefault(camera_name, len(camera_order))

    if ignore_metadata:
        return sorted(
            target_files,
            key=lambda path: (
                camera_order.get(camera_map.get(str(path)) or path.parent.name or "CAM 01", 0),
                natural_path_key(path),
                str(path).casefold(),
            ),
        )

    return sorted(
        target_files,
        key=lambda path: (
            camera_order.get(camera_map.get(str(path)) or path.parent.name or "CAM 01", 0),
            path.stat().st_mtime,
            natural_path_key(path),
            str(path).casefold(),
        ),
    )


def natural_path_key(path: Path) -> tuple[tuple[int, object], ...]:
    parts = re.split(r"(\d+)", path.name.casefold())
    return tuple((1, int(part)) if part.isdigit() else (0, part) for part in parts)


def build_camera_map(target_files: list[Path], targets_roots: list[Path]) -> dict[str, str]:
    camera_map: dict[str, str] = {}
    roots = [root.resolve() for root in targets_roots if root.is_dir()]

    for target_file in target_files:
        target = target_file.resolve()
        camera_name = identify_camera_name(target)

        if camera_name is None:
            camera_name = target.parent.name or "CAM 01"
            for root in roots:
                try:
                    relative = target.relative_to(root)
                    camera_name = relative.parts[0] if len(relative.parts) > 1 else root.name
                    break
                except ValueError:
                    continue

        camera_map[str(target)] = camera_name or "CAM 01"

    return camera_map


def identify_camera_name(path: Path) -> str | None:
    camera_folder = camera_folder_name_from_path(path)
    if camera_folder:
        return camera_folder

    text = " ".join([path.name, *path.parts]).casefold()
    if "a7iv" in text or "victor" in text:
        return "CAM 01 - A7IV - VICTOR"
    if "zve10" in text or "zv-e10" in text:
        if "kenia" in text:
            return "CAM 02 - ZVE10 - KENIA"
        if "kaiky" in text:
            return "CAM 03 - ZVE10 - KAIKY"
        return "CAM 02 - ZVE10 - KAIKY"
    if "a6600" in text or "gui" in text:
        return "CAM 04 - A6600 - GUI"
    return None


def camera_folder_name_from_path(path: Path) -> str | None:
    for part in reversed(path.parts):
        text = part.strip()
        if re.match(r"^cam\s+\d+\b", text, flags=re.IGNORECASE):
            return text
    return None


def scan_files(root: Path, extensions: set[str]) -> list[Path]:
    files: list[Path] = []
    for candidate in root.rglob("*"):
        if candidate.is_file() and not is_ignored_path(candidate):
            if candidate.suffix.lower() in extensions:
                files.append(candidate.resolve())
    return sorted(files, key=lambda item: str(item).casefold())


def is_ignored_path(path: Path) -> bool:
    for part in path.parts:
        if part.startswith("."):
            return True

    name = path.name
    lower_name = name.lower()
    is_windows_hidden = bool(getattr(path.stat(), "st_file_attributes", 0) & 0x2)
    return (
        is_windows_hidden
        or name.startswith("~")
        or lower_name.endswith(".tmp")
        or path.suffix.lower() in TEMPORARY_SUFFIXES
    )


def normalize_timebase(fps_value: str) -> str:
    try:
        fps = float(str(fps_value).replace(",", "."))
    except ValueError as exc:
        raise ValueError(f"FPS invalido: {fps_value}") from exc

    if fps <= 0:
        raise ValueError("FPS deve ser maior que zero.")

    return str(int(round(fps)))


def ensure_temp_dir() -> None:
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    AUDIO_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def cleanup_temp_wavs() -> int:
    if not TEMP_DIR.exists():
        return 0

    removed = 0
    for wav_file in TEMP_DIR.glob("*.wav"):
        try:
            wav_file.unlink()
            removed += 1
        except OSError as exc:
            logger.warning("Nao foi possivel remover temporario %s: %s", wav_file, exc)
    return removed


def cleanup_cache_directory() -> tuple[int, int]:
    """
    Remove todo o cache DSP extraido/calculado.

    A remocao do cache nao muda a precisao do sync; apenas forca o proximo run
    a reextrair WAVs e recalcular features a partir dos arquivos originais.
    """
    if not CACHE_ROOT_DIR.exists():
        ensure_temp_dir()
        return 0, 0

    cache_root = CACHE_ROOT_DIR.resolve()
    temp_root = TEMP_DIR.resolve()
    try:
        cache_root.relative_to(temp_root)
    except ValueError as exc:
        raise RuntimeError(f"Diretorio de cache inseguro para limpeza: {cache_root}") from exc

    file_count = 0
    total_bytes = 0
    for item in CACHE_ROOT_DIR.rglob("*"):
        if item.is_file():
            file_count += 1
            try:
                total_bytes += item.stat().st_size
            except OSError:
                pass

    shutil.rmtree(CACHE_ROOT_DIR)
    ensure_temp_dir()
    return file_count, total_bytes


def read_cache_cleanup_state() -> dict:
    if not CACHE_CLEANUP_STATE_PATH.exists():
        return {}
    try:
        payload = json.loads(CACHE_CLEANUP_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_cache_cleanup_state(state: dict) -> None:
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_CLEANUP_STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def update_cache_cleanup_after_completed_sync() -> dict:
    """
    Limpa o cache automaticamente apos cada sincronizacao concluida.

    O contador fica fora de `temp/cache`, entao sobrevivera a limpeza do cache.
    """
    state = read_cache_cleanup_state()
    completed_since_cleanup = int(state.get("completed_since_cleanup") or 0) + 1
    total_completed_syncs = int(state.get("total_completed_syncs") or 0) + 1

    result = {
        "enabled": True,
        "interval_syncs": CACHE_CLEANUP_INTERVAL_SYNCS,
        "completed_since_cleanup": completed_since_cleanup,
        "total_completed_syncs": total_completed_syncs,
        "cleanup_performed": False,
        "removed_files": 0,
        "removed_bytes": 0,
    }

    if completed_since_cleanup >= CACHE_CLEANUP_INTERVAL_SYNCS:
        removed_files, removed_bytes = cleanup_cache_directory()
        completed_since_cleanup = 0
        result.update(
            {
                "completed_since_cleanup": completed_since_cleanup,
                "cleanup_performed": True,
                "removed_files": removed_files,
                "removed_bytes": removed_bytes,
                "last_cleanup_at": datetime.now().isoformat(timespec="seconds"),
            }
        )

    state.update(result)
    write_cache_cleanup_state(state)
    return result


def log_cache_cleanup_result(result: dict) -> None:
    if result.get("cleanup_performed"):
        removed_bytes = int(result.get("removed_bytes") or 0)
        logger.info(
            "Auto cache cleanup: cache limpo apos sync concluido: %d arquivo(s), %.2f GiB removidos.",
            int(result.get("removed_files") or 0),
            removed_bytes / (1024.0**3),
        )
        return

    logger.info(
        "Auto cache cleanup: cache sera limpo apos cada sync concluido."
    )


def prepare_reference_tracks(
    reference_files: list[Path],
    *,
    ignore_metadata: bool,
) -> list[PreparedReference]:
    prepared: list[dict] = []
    for index, reference_file in enumerate(reference_files, start=1):
        logger.info("Preparando referencia %d/%d: %s", index, len(reference_files), reference_file.name)
        cached_audio = prepare_cached_audio_features(
            reference_file,
            AUDIO_CACHE_DIR,
            label=f"reference_{index - 1}_{reference_file.stem}",
        )
        reference_wav = cached_audio.wav_path
        features = cached_audio.features
        absolute_start_time = media_absolute_start_time(reference_file, features.duration_seconds)
        prepared.append(
            {
                "path": reference_file,
                "key": str(reference_file),
                "name": infer_reference_name(reference_file, index),
                "track_name": "",
                "wav_path": reference_wav,
                "duration_seconds": features.duration_seconds,
                "absolute_start_time": absolute_start_time,
                "features": features,
                "original_index": index,
                "cache_hit_wav": cached_audio.cache_hit_wav,
                "cache_hit_features": cached_audio.cache_hit_features,
            }
        )

    if not prepared:
        raise ValueError("Nenhuma referencia de audio preparada.")

    assign_reference_track_names(prepared)
    aligned_offsets = align_reference_offsets(prepared)
    for item in prepared:
        item["timeline_offset_seconds"] = aligned_offsets[item["key"]]
        logger.info(
            "Referencia calibrada: %s | track=%s | offset_rel_primary %.6fs",
            Path(item["path"]).name,
            item["track_name"],
            item["timeline_offset_seconds"],
        )

    return [
        PreparedReference(
            path=item["path"],
            key=item["key"],
            name=item["name"],
            track_name=item["track_name"],
            wav_path=item["wav_path"],
            duration_seconds=float(item["duration_seconds"]),
            absolute_start_time=float(item["absolute_start_time"]),
            timeline_offset_seconds=float(item["timeline_offset_seconds"]),
            features=item["features"],
            original_index=int(item["original_index"]),
            cache_hit_wav=bool(item.get("cache_hit_wav")),
            cache_hit_features=bool(item.get("cache_hit_features")),
        )
        for item in prepared
    ]


def assign_reference_track_names(prepared: list[dict]) -> None:
    continuity_links = reference_continuity_links(prepared)
    child_keys = set(continuity_links)
    children_by_parent: dict[str, list[dict]] = {}
    by_key = {item["key"]: item for item in prepared}

    for child_key, parent_key in continuity_links.items():
        children_by_parent.setdefault(parent_key, []).append(by_key[child_key])

    roots = [
        item
        for item in sorted(prepared, key=lambda value: (value["absolute_start_time"], value["original_index"]))
        if item["key"] not in child_keys
    ]

    for track_index, root in enumerate(roots, start=1):
        track_name = infer_reference_track_name(root["path"], track_index)
        assign_reference_track_name_recursively(root, track_name, children_by_parent)


def assign_reference_track_name_recursively(
    item: dict,
    track_name: str,
    children_by_parent: dict[str, list[dict]],
) -> None:
    item["track_name"] = track_name
    for child in sorted(
        children_by_parent.get(item["key"], []),
        key=lambda value: (value["absolute_start_time"], value["original_index"]),
    ):
        assign_reference_track_name_recursively(child, track_name, children_by_parent)


def infer_reference_track_name(reference_file: Path, index: int) -> str:
    lapela_number = infer_lapela_number(reference_file)
    if lapela_number is not None:
        return f"Lapela {lapela_number:02d}"
    return infer_non_lapel_reference_name(reference_file, index)


def align_reference_offsets(prepared: list[dict]) -> dict[str, float]:
    primary = prepared[0]
    edges = build_reference_alignment_edges(prepared)
    offsets = {primary["key"]: 0.0}

    ordered_edges = sorted(
        edges,
        key=lambda edge: (edge.priority, -edge.score, edge.method, edge.target_key),
    )
    while True:
        made_progress = False
        for edge in ordered_edges:
            if edge.source_key not in offsets or edge.target_key in offsets:
                continue
            offsets[edge.target_key] = offsets[edge.source_key] + edge.delta_seconds
            logger.info(
                "Offset de referencia resolvido por %s: %s -> %.6fs",
                edge.method,
                Path(edge.target_key).name,
                offsets[edge.target_key],
            )
            made_progress = True
        if not made_progress:
            break

    primary_start = primary["absolute_start_time"]
    for item in prepared:
        if item["key"] in offsets:
            continue
        fallback_offset = item["absolute_start_time"] - primary_start
        offsets[item["key"]] = fallback_offset
        logger.warning(
            "Referencia sem caminho de alinhamento por audio/continuidade: %s. "
            "Usando fallback por horario real: %.6fs",
            Path(item["path"]).name,
            fallback_offset,
        )

    return offsets


def build_reference_alignment_edges(prepared: list[dict]) -> list[ReferenceAlignmentEdge]:
    edges: list[ReferenceAlignmentEdge] = []
    edges.extend(reference_audio_alignment_edges(prepared))
    edges.extend(reference_continuity_edges(prepared))
    return edges


def reference_audio_alignment_edges(prepared: list[dict]) -> list[ReferenceAlignmentEdge]:
    edges: list[ReferenceAlignmentEdge] = []
    for left_index, left in enumerate(prepared):
        for right in prepared[left_index + 1 :]:
            overlap_seconds = reference_overlap_seconds(left, right)
            ignore_mtime = should_ignore_reference_mtime_for_alignment(left, right)
            if overlap_seconds < MIN_REFERENCE_OVERLAP_SECONDS and not ignore_mtime:
                continue

            result = correlate_feature_envelopes_full_scan(
                left["features"].normalized_envelope,
                right["features"].normalized_envelope,
                feature_rate=left["features"].feature_rate,
            )
            delta_seconds = correlation_offset_to_premiere_offset(result.offset_seconds)
            expected_delta = right["absolute_start_time"] - left["absolute_start_time"]
            deviation = abs(delta_seconds - expected_delta)

            if result.z_score < MIN_REFERENCE_ALIGNMENT_Z_SCORE:
                logger.warning(
                    "Alinhamento entre lapelas fraco: %s <-> %s | z=%.2f. "
                    "Ignorando aresta de audio.",
                    Path(left["path"]).name,
                    Path(right["path"]).name,
                    result.z_score,
                )
                continue

            if result.prominence_ratio < MIN_CONFIDENCE_PROMINENCE:
                logger.warning(
                    "Alinhamento entre referencias sem proeminencia: %s <-> %s | "
                    "prom=%.3f. Ignorando aresta de audio.",
                    Path(left["path"]).name,
                    Path(right["path"]).name,
                    result.prominence_ratio,
                )
                continue

            if not ignore_mtime and deviation > MAX_REFERENCE_ALIGNMENT_DEVIATION_SECONDS:
                logger.warning(
                    "Alinhamento entre lapelas divergente demais: %s <-> %s | "
                    "audio %.3fs vs horario %.3fs. Ignorando provavel falso positivo.",
                    Path(left["path"]).name,
                    Path(right["path"]).name,
                    delta_seconds,
                    expected_delta,
                )
                continue

            if ignore_mtime:
                logger.warning(
                    "Aresta de audio sem confiar em mtime: %s -> %s | "
                    "delta %.6fs | z=%.2f | prom=%.3f",
                    Path(left["path"]).name,
                    Path(right["path"]).name,
                    delta_seconds,
                    result.z_score,
                    result.prominence_ratio,
                )
            else:
                logger.info(
                    "Aresta de audio entre lapelas: %s -> %s | delta %.6fs | z=%.2f",
                    Path(left["path"]).name,
                    Path(right["path"]).name,
                    delta_seconds,
                    result.z_score,
                )

            edges.append(
                ReferenceAlignmentEdge(
                    source_key=left["key"],
                    target_key=right["key"],
                    delta_seconds=delta_seconds,
                    priority=1,
                    score=result.z_score,
                    method="audio_reference_correlation",
                )
            )
            edges.append(
                ReferenceAlignmentEdge(
                    source_key=right["key"],
                    target_key=left["key"],
                    delta_seconds=-delta_seconds,
                    priority=1,
                    score=result.z_score,
                    method="audio_reference_correlation",
                )
            )

    return edges


def should_ignore_reference_mtime_for_alignment(left: dict, right: dict) -> bool:
    """
    Fontes externas, como mesa/H4N, muitas vezes chegam com mtime fora da
    regua dos DJI. Para elas, o audio e a autoridade; para lapelas DJI, o mtime
    ainda serve para evitar full scan falso positivo em musica/ruido.
    """
    return is_external_reference_source(left["path"]) or is_external_reference_source(
        right["path"]
    )


def is_external_reference_source(path: Path) -> bool:
    if infer_lapela_number(path) is not None:
        return False

    text = " ".join([path.stem, *path.parts]).casefold()
    return any(token in text for token in ("mesa", "h4n", "mono", "zoom", "recorder"))


def reference_continuity_edges(prepared: list[dict]) -> list[ReferenceAlignmentEdge]:
    by_key = {item["key"]: item for item in prepared}
    edges: list[ReferenceAlignmentEdge] = []
    for child_key, parent_key in reference_continuity_links(prepared).items():
        parent = by_key[parent_key]
        child = by_key[child_key]
        gap_seconds = reference_gap(parent, child)
        delta_seconds = parent["duration_seconds"]
        score = max(1.0, REFERENCE_CONTINUITY_TOLERANCE_SECONDS - abs(gap_seconds))
        logger.info(
            "Aresta de continuidade de lapela: %s -> %s | delta %.6fs | gap_mtime %.6fs",
            Path(parent["path"]).name,
            Path(child["path"]).name,
            delta_seconds,
            gap_seconds,
        )
        edges.append(
            ReferenceAlignmentEdge(
                source_key=parent["key"],
                target_key=child["key"],
                delta_seconds=delta_seconds,
                priority=0,
                score=score,
                method="same_recorder_continuation",
            )
        )
        edges.append(
            ReferenceAlignmentEdge(
                source_key=child["key"],
                target_key=parent["key"],
                delta_seconds=-delta_seconds,
                priority=0,
                score=score,
                method="same_recorder_continuation",
            )
        )
    return edges


def reference_continuity_links(prepared: list[dict]) -> dict[str, str]:
    links: dict[str, str] = {}
    sorted_items = sorted(prepared, key=lambda item: (item["absolute_start_time"], item["original_index"]))

    for child in sorted_items:
        candidates: list[tuple[float, dict]] = []
        for parent in sorted_items:
            if parent["key"] == child["key"]:
                continue
            if parent["absolute_start_time"] >= child["absolute_start_time"]:
                continue
            if not is_likely_reference_continuation(parent, child):
                continue
            gap = reference_gap(parent, child)
            if abs(gap) <= REFERENCE_CONTINUITY_TOLERANCE_SECONDS:
                candidates.append((abs(gap), parent))

        if candidates:
            _gap, parent = min(candidates, key=lambda value: (value[0], value[1]["original_index"]))
            links[child["key"]] = parent["key"]

    return links


def is_likely_reference_continuation(parent: dict, child: dict) -> bool:
    parent_sequence = reference_sequence_number(parent["path"])
    child_sequence = reference_sequence_number(child["path"])
    if parent_sequence is not None and child_sequence is not None:
        return child_sequence == parent_sequence + 1
    return False


def reference_sequence_number(path: Path) -> int | None:
    match = re.match(r"(?:DJI|MIC|REC)[_-]?0*(\d+)", path.stem, flags=re.IGNORECASE)
    if not match:
        return None
    return int(match.group(1))


def reference_gap(parent: dict, child: dict) -> float:
    return child["absolute_start_time"] - (
        parent["absolute_start_time"] + parent["duration_seconds"]
    )


def reference_overlap_seconds(left: dict, right: dict) -> float:
    left_start = left["absolute_start_time"]
    left_end = left_start + left["duration_seconds"]
    right_start = right["absolute_start_time"]
    right_end = right_start + right["duration_seconds"]
    return max(0.0, min(left_end, right_end) - max(left_start, right_start))


def infer_reference_name(reference_file: Path, index: int) -> str:
    lapela_number = infer_lapela_number(reference_file)
    if lapela_number is not None:
        return f"Lapela {lapela_number:02d}"
    return infer_non_lapel_reference_name(reference_file, index)


def infer_lapela_number(reference_file: Path) -> int | None:
    full_text = " ".join([reference_file.stem, *reference_file.parts])
    match = re.search(r"lapela\D*0*(\d+)", full_text, flags=re.IGNORECASE)
    if match:
        return int(match.group(1))

    # Fallback para lotes sem pasta "Lapela NN". Em gravadores DJI, o numero
    # do arquivo tambem pode ser sequencial, entao este fallback so deve ser
    # usado quando a estrutura de pastas nao informar a lapela fisica.
    match = re.match(r"(?:DJI|MIC|REC)[_-]?0*(\d+)(?:[_-]|$)", reference_file.stem, flags=re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


def infer_non_lapel_reference_name(reference_file: Path, index: int) -> str:
    parent_parts = reference_file.parts[:-1]
    for part in reversed(parent_parts):
        normalized = part.strip()
        lowered = normalized.casefold()
        if "mesa" in lowered or "h4n" in lowered:
            return clean_reference_source_name(normalized)

    for part in reversed(parent_parts):
        normalized = part.strip()
        lowered = normalized.casefold()
        if "mono" in lowered:
            return clean_reference_source_name(normalized)

    return clean_reference_source_name(reference_file.stem) or f"Referencia {index:02d}"


def clean_reference_source_name(value: str) -> str:
    cleaned = re.sub(r"^audio\s*[-_]\s*", "", value.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -_")
    return cleaned or value.strip()


def find_best_reference_match(
    references: list[PreparedReference],
    target_features: object,
    *,
    master_reference: PreparedReference | None = None,
    estimated_master_offset_seconds: float | None = None,
) -> tuple[PreparedReference, CorrelationResult]:
    full_scan_matches: list[tuple[float, float, int, PreparedReference, CorrelationResult]] = []
    assisted_matches: list[tuple[float, float, int, PreparedReference, CorrelationResult]] = []
    for reference in references:
        full_result = correlate_feature_envelopes_full_scan(
            reference.features.normalized_envelope,
            target_features.normalized_envelope,
            feature_rate=reference.features.feature_rate,
        )
        full_scan_matches.append(
            (
                full_result.z_score,
                full_result.prominence_ratio,
                -reference.original_index,
                reference,
                full_result,
            )
        )

        if master_reference is None or estimated_master_offset_seconds is None:
            continue
        reference_delta = reference_offset_from_master(reference, master_reference)
        estimated_correlation_offset = reference_delta - estimated_master_offset_seconds
        try:
            assisted_result = correlate_feature_envelopes(
                reference.features.normalized_envelope,
                target_features.normalized_envelope,
                feature_rate=reference.features.feature_rate,
                estimated_offset=estimated_correlation_offset,
                window_seconds=IGNORE_METADATA_ASSIST_WINDOW_SECONDS,
                source="metadata_assisted_full_scan",
            )
        except ValueError:
            continue
        assisted_matches.append(
            (
                assisted_result.z_score,
                assisted_result.prominence_ratio,
                -reference.original_index,
                reference,
                assisted_result,
            )
        )

    _z_score, _prominence, _index, reference, result = max(
        full_scan_matches,
        key=lambda item: item[:3],
    )

    if assisted_matches and estimated_master_offset_seconds is not None:
        assisted_reference, assisted_result = best_metadata_assisted_match(
            assisted_matches,
            result,
            estimated_master_offset_seconds,
            master_reference,
        )
        if assisted_result is not None:
            return assisted_reference, assisted_result

    return reference, result


def best_metadata_assisted_match(
    assisted_matches: list[tuple[float, float, int, PreparedReference, CorrelationResult]],
    full_scan_result: CorrelationResult,
    estimated_master_offset_seconds: float,
    master_reference: PreparedReference | None,
) -> tuple[PreparedReference, CorrelationResult | None]:
    _z_score, _prominence, _index, reference, result = max(
        assisted_matches,
        key=lambda item: item[:3],
    )
    if result.z_score < MIN_FULL_SCAN_Z_SCORE:
        return reference, None
    if result.prominence_ratio < MIN_CONFIDENCE_PROMINENCE:
        return reference, None
    if master_reference is None:
        return reference, result

    full_scan_premiere_offset = correlation_offset_to_premiere_offset(
        full_scan_result.offset_seconds
    )
    full_deviation = abs(full_scan_premiere_offset - estimated_master_offset_seconds)
    if full_deviation >= IGNORE_METADATA_ASSIST_MAX_FULL_SCAN_DEVIATION_SECONDS:
        return reference, result
    if result.z_score >= full_scan_result.z_score * 0.9:
        return reference, result
    return reference, None

def select_master_reference_track(
    references: list[PreparedReference],
) -> tuple[PreparedReference, list[PreparedReference]]:
    """
    Fixa a regua de tempo numa trilha de lapela, nao numa parte isolada qualquer.

    A primeira parte da trilha principal, normalmente Lapela 01 / DJI_01,
    vira a ancora zero. As continuacoes da mesma trilha continuam elegiveis
    para correlacao dos videos longos, mas cameras nao podem trocar para outra
    lapela fisica durante o lote.
    """
    master_anchor = fallback_master_reference(references)
    master_track_name = master_anchor.track_name
    master_parts = [
        reference for reference in references if reference.track_name == master_track_name
    ]
    master_parts.sort(key=lambda reference: (reference.timeline_offset_seconds, reference.original_index))
    if not master_parts:
        master_parts = [master_anchor]

    master_anchor = master_parts[0]
    logger.info(
        "MASTER_TRACK fixada: %s | ancora=%s | partes=%d",
        master_track_name,
        master_anchor.path.name,
        len(master_parts),
    )
    return master_anchor, master_parts


def media_absolute_start_time(path: Path, duration_seconds: float) -> float:
    filename_start = reference_start_time_from_filename(path)
    if filename_start is not None:
        return filename_start
    return path.stat().st_mtime - float(duration_seconds)


def reference_start_time_from_filename(path: Path) -> float | None:
    match = re.search(r"(20\d{6})[_-]?(\d{6})", path.stem)
    if not match:
        return None
    try:
        return datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S").timestamp()
    except ValueError:
        return None


def choose_master_reference(
    references: list[PreparedReference],
    matches: list[ClipSyncMatch],
) -> PreparedReference:
    score_by_reference = {
        reference.key: {
            "z_score_total": 0.0,
            "best_z_score": 0.0,
            "match_count": 0,
            "reference": reference,
        }
        for reference in references
    }

    for match in matches:
        score = score_by_reference[match.chosen_reference.key]
        score["z_score_total"] += match.chosen_result.z_score
        score["best_z_score"] = max(score["best_z_score"], match.chosen_result.z_score)
        score["match_count"] += 1

    scored = [score for score in score_by_reference.values() if score["match_count"] > 0]
    if scored:
        winner = max(
            scored,
            key=lambda score: (
                score["z_score_total"],
                score["best_z_score"],
                score["match_count"],
                -score["reference"].original_index,
            ),
        )
        master = winner["reference"]
        logger.info(
            "MASTER_REF eleita por Z-score acumulado: %s | total=%.2f | melhor=%.2f | matches=%d",
            master.name,
            winner["z_score_total"],
            winner["best_z_score"],
            winner["match_count"],
        )
        return master

    master = fallback_master_reference(references)
    logger.warning(
        "Nao foi possivel eleger MASTER_REF por matches validos. Usando fallback: %s",
        master.name,
    )
    return master


def fallback_master_reference(references: list[PreparedReference]) -> PreparedReference:
    def priority(reference: PreparedReference) -> tuple[int, float, int]:
        # Use only explicit source identity here. Track names can be auto-generated
        # for non-lapel sources such as mixer audio and should not elect the master.
        text = f"{reference.name} {reference.path}".casefold()
        is_main_lapela = bool(
            re.search(r"lapela\D*0*1\b", text)
            or re.search(r"dji[_\s-]*0*1\b", text)
        )
        return (0 if is_main_lapela else 1, reference.absolute_start_time, reference.original_index)

    return min(references, key=priority)


def reference_offset_from_master(
    reference: PreparedReference,
    master_reference: PreparedReference,
) -> float:
    return reference.timeline_offset_seconds - master_reference.timeline_offset_seconds


def reference_result_entries(
    references: list[PreparedReference],
    master_reference: PreparedReference,
) -> list[dict]:
    return [
        {
            "path": str(reference.path),
            "name": reference.name,
            "track_name": reference.track_name,
            "duration_seconds": reference.duration_seconds,
            "repaired_duration_seconds": reference.duration_seconds,
            "absolute_start_time": reference.absolute_start_time,
            "timeline_offset_seconds": reference_offset_from_master(reference, master_reference),
            "extracted_wav_path": str(reference.wav_path),
            "duration_source": "extracted_wav_samples",
            "cache_hit_wav": reference.cache_hit_wav,
            "cache_hit_features": reference.cache_hit_features,
            "is_primary": reference.original_index == 1,
            "is_master": reference.key == master_reference.key,
        }
        for reference in references
    ]


def average_reference_mtime(reference_files: list[Path]) -> float:
    return sum(path.stat().st_mtime for path in reference_files) / float(len(reference_files))


def mtime_gap_seconds(path: Path, reference_mtime_average: float) -> float:
    return abs(path.stat().st_mtime - reference_mtime_average)


def register_skipped_clip(
    results: dict,
    clip: PreparedClip,
    *,
    reason: str,
    extra_metadata: dict | None = None,
) -> None:
    results["offsets"][clip.key] = None
    metadata = {
        "duration_seconds": clip.duration_seconds,
        "repaired_duration_seconds": clip.duration_seconds,
        "duration_source": "extracted_wav_samples",
        "extracted_wav_path": str(clip.wav_path),
        "cache_hit_wav": clip.cache_hit_wav,
        "cache_hit_features": clip.cache_hit_features,
        "camera_name": clip.camera_name,
        "sync_skipped": True,
        "skip_reason": reason,
    }
    if extra_metadata:
        metadata.update(extra_metadata)
    results["metadata"][clip.key] = metadata


def register_unprepared_target(
    results: dict,
    target_file: Path,
    *,
    camera_map: dict[str, str],
    original_index: int,
    reason: str,
    error: str,
    ignore_metadata: bool,
) -> None:
    key = str(target_file)
    camera_name = camera_map.get(key) or target_file.parent.name or "CAM 01"
    try:
        target_mtime = target_file.stat().st_mtime
    except OSError:
        target_mtime = None

    results["offsets"][key] = None
    results["metadata"][key] = {
        "duration_seconds": None,
        "repaired_duration_seconds": None,
        "duration_source": "unavailable",
        "extracted_wav_path": None,
        "cache_hit_wav": False,
        "cache_hit_features": False,
        "estimated_start_time": None,
        "estimated_offset_seconds": None,
        "camera_name": camera_name,
        "sync_skipped": True,
        "skip_reason": reason,
        "error": error,
        "target_prepare_failed": True,
        "target_original_index": original_index,
        "target_mtime": target_mtime,
        "metadata_ignored": ignore_metadata,
    }


def sync_multiple_tracks_hybrid(
    reference_files: list[Path],
    target_files: list[Path],
    camera_map: dict[str, str],
    ignore_metadata: bool = False,
    camera_global_offset_seconds: float = 0.0,
    camera_offset_seconds_by_name: dict[str, float] | None = None,
    use_camera_clock_model: bool = False,
    explicit_selection: bool = False,
) -> dict:
    if ignore_metadata:
        logger.warning(
            "Modo contingencia ativo: ignorando mtime/metadados e usando Full Scan total."
        )
    else:
        logger.info(
            "Modo hibrido ativo: busca estrita em janela de metadados +/- %.0fs.",
            HYBRID_WINDOW_SECONDS,
        )

    references = prepare_reference_tracks(reference_files, ignore_metadata=ignore_metadata)
    master_reference, master_reference_parts = select_master_reference_track(references)
    primary_reference = master_reference
    reference_mtime_average = average_reference_mtime(reference_files)
    reference_start = primary_reference.absolute_start_time

    results = {
        "reference": str(primary_reference.path),
        "references": reference_result_entries(references, primary_reference),
        "offsets": {},
        "metadata": {
            "reference_duration_seconds": primary_reference.duration_seconds,
            "reference_repaired_duration_seconds": primary_reference.duration_seconds,
            "reference_extracted_wav_path": str(primary_reference.wav_path),
            "reference_duration_source": "extracted_wav_samples",
            "reference_estimated_start_time": reference_start,
            "reference_count": len(references),
            "audio_cache_dir": str(AUDIO_CACHE_DIR),
            "reference_cache_hit_wav_count": sum(1 for reference in references if reference.cache_hit_wav),
            "reference_cache_hit_features_count": sum(1 for reference in references if reference.cache_hit_features),
            "reference_match_scope": "all_references_master_unified",
            "master_reference_path": str(primary_reference.path),
            "master_reference_name": primary_reference.name,
            "master_reference_track_name": primary_reference.track_name,
            "master_reference_start_time": primary_reference.absolute_start_time,
            "timeline_anchor_start_time": primary_reference.absolute_start_time,
            "offsets_are_master_unified": False,
            "master_reference_part_count": len(master_reference_parts),
            "camera_global_offset_seconds": camera_global_offset_seconds,
            "camera_offset_seconds_by_name": camera_offset_seconds_by_name or {},
            "use_camera_clock_model": use_camera_clock_model,
            "camera_post_cut_native_late_threshold_seconds": (
                CAMERA_POST_CUT_NATIVE_LATE_THRESHOLD_SECONDS
            ),
            "camera_post_cut_native_min_previous_duration_seconds": (
                CAMERA_POST_CUT_NATIVE_MIN_PREVIOUS_DURATION_SECONDS
            ),
            "camera_post_cut_native_max_gap_seconds": (
                CAMERA_POST_CUT_NATIVE_MAX_GAP_SECONDS
            ),
            "camera_peer_refine_window_seconds": CAMERA_PEER_REFINE_WINDOW_SECONDS,
            "camera_peer_refine_max_delta_seconds": CAMERA_PEER_REFINE_MAX_DELTA_SECONDS,
            "camera_peer_refine_min_z_score": CAMERA_PEER_REFINE_MIN_Z_SCORE,
            "camera_peer_refine_min_prominence": CAMERA_PEER_REFINE_MIN_PROMINENCE,
            "camera_peer_refine_min_overlap_seconds": CAMERA_PEER_REFINE_MIN_OVERLAP_SECONDS,
            "camera_audio_source": "embedded_mp4",
            "camera_audio_uses_extracted_wav": False,
            "explicit_selection": explicit_selection,
            "mtime_safety_gate_enabled": not explicit_selection,
            "sync_mode": (
                "ignore_metadata_full_scan_transient_correlation"
                if ignore_metadata
                else "strict_metadata_window_audio_fine_tune"
            ),
        },
        "spanning_groups": [],
    }

    prepared_clips = prepare_target_clips(
        target_files=sort_targets_for_batch(
            target_files,
            camera_map,
            ignore_metadata=ignore_metadata,
        ),
        reference_start=reference_start,
        camera_map=camera_map,
        ignore_metadata=ignore_metadata,
        results=results,
    )

    matches: list[ClipSyncMatch] = []
    for index, clip in enumerate(prepared_clips, start=1):
        try:
            if ignore_metadata and not explicit_selection:
                gap_seconds = mtime_gap_seconds(clip.path, reference_mtime_average)
                if gap_seconds > MAX_TIME_GAP_SECONDS:
                    logger.warning(
                        "Descartando %s por incompatibilidade cronologica: "
                        "gap mtime %.1fs > %.1fs.",
                        clip.path.name,
                        gap_seconds,
                        MAX_TIME_GAP_SECONDS,
                    )
                    register_skipped_clip(
                        results,
                        clip,
                        reason="mtime_gap_exceeds_block_limit",
                        extra_metadata={
                            "metadata_ignored": True,
                            "target_mtime": clip.path.stat().st_mtime,
                            "reference_mtime_average": reference_mtime_average,
                            "mtime_gap_seconds": gap_seconds,
                            "max_time_gap_seconds": MAX_TIME_GAP_SECONDS,
                        },
                    )
                    continue
            elif ignore_metadata and explicit_selection and index == 1:
                logger.warning(
                    "Selecao explicita ativa: trava de incompatibilidade por mtime "
                    "desabilitada para preservar os arquivos escolhidos pelo usuario."
                )

            target_features = clip.features

            if ignore_metadata:
                logger.info(
                    "Full Scan %d/%d: %s | metadados ignorados | %d referencia(s)",
                    index,
                    len(prepared_clips),
                    clip.path.name,
                    len(references),
                )
                chosen_reference, chosen_result = find_best_reference_match(
                    references,
                    target_features,
                    master_reference=master_reference,
                    estimated_master_offset_seconds=clip.estimated_offset_seconds,
                )
                window_result = None
                if chosen_result.z_score < MIN_FULL_SCAN_Z_SCORE:
                    if explicit_selection and use_camera_clock_model:
                        logger.warning(
                            "Casamento fraco para %s: z-score %.2f < %.2f. "
                            "Mantendo clip selecionado para posicionamento pelo modelo da camera.",
                            clip.path.name,
                            chosen_result.z_score,
                            MIN_FULL_SCAN_Z_SCORE,
                        )
                    else:
                        logger.warning(
                            "Casamento fraco para %s: z-score %.2f < %.2f. Clip ignorado.",
                            clip.path.name,
                            chosen_result.z_score,
                            MIN_FULL_SCAN_Z_SCORE,
                        )
                        register_skipped_clip(
                            results,
                            clip,
                            reason="weak_full_scan_match",
                            extra_metadata={
                                "metadata_ignored": True,
                                "chosen_reference_path": str(chosen_reference.path),
                                "chosen_reference_name": chosen_reference.name,
                                "correlation_source": chosen_result.source,
                                "correlation_z_score": chosen_result.z_score,
                                "correlation_prominence_ratio": chosen_result.prominence_ratio,
                                "min_full_scan_z_score": MIN_FULL_SCAN_Z_SCORE,
                            },
                        )
                        continue
            else:
                chosen_reference = primary_reference
                logger.info(
                    "Correlacionando %d/%d: %s | offset_estimado=%.3fs",
                    index,
                    len(prepared_clips),
                    clip.path.name,
                    clip.estimated_offset_seconds,
                )
                window_result = correlate_feature_envelopes(
                    primary_reference.features.normalized_envelope,
                    target_features.normalized_envelope,
                    feature_rate=primary_reference.features.feature_rate,
                    estimated_offset=clip.estimated_offset_seconds,
                    window_seconds=HYBRID_WINDOW_SECONDS,
                    source="metadata_window",
                )

                chosen_result = window_result
                if window_result.low_confidence:
                    logger.warning(
                        "Pico de janela com baixa confianca em %s (z=%.2f, prom=%.3f). "
                        "Mantendo pico local da janela de metadados.",
                        clip.path.name,
                        window_result.z_score,
                        window_result.prominence_ratio,
                    )

            matches.append(
                ClipSyncMatch(
                    clip=clip,
                    chosen_reference=chosen_reference,
                    chosen_result=chosen_result,
                    window_result=window_result,
                    target_features=target_features,
                    metadata_ignored=ignore_metadata,
                )
            )
        except Exception as exc:
            logger.error("Erro ao sincronizar %s: %s", clip.path, exc)
            register_skipped_clip(
                results,
                clip,
                reason="sync_error",
                extra_metadata={"error": str(exc), "metadata_ignored": ignore_metadata},
            )

    selected_matches = list(matches)
    invisible_anchor_support = {
        "enabled": bool(explicit_selection and ignore_metadata and use_camera_clock_model),
        "triggered": False,
        "used": False,
        "max_extra_clips_per_camera": INVISIBLE_ANCHOR_MAX_EXTRA_CLIPS_PER_CAMERA,
        "forward_radius": INVISIBLE_ANCHOR_FORWARD_RADIUS,
        "backward_radius": INVISIBLE_ANCHOR_BACKWARD_RADIUS,
        "near_forward_count": INVISIBLE_ANCHOR_NEAR_FORWARD_COUNT,
        "long_forward_count": INVISIBLE_ANCHOR_LONG_FORWARD_COUNT,
        "backward_count": INVISIBLE_ANCHOR_BACKWARD_COUNT,
        "cameras_requiring_support": [],
        "candidate_files_by_camera": {},
        "accepted_files_by_camera": {},
        "accepted_count": 0,
    }
    if explicit_selection and ignore_metadata and use_camera_clock_model and selected_matches:
        risky_cameras = cameras_missing_visible_anchor(selected_matches, master_reference)
        invisible_anchor_support["cameras_requiring_support"] = risky_cameras
        if risky_cameras:
            invisible_anchor_support["triggered"] = True
            logger.warning(
                "Apoio invisivel ativado: camera(s) sem ancora elegivel na selecao: %s",
                ", ".join(risky_cameras),
            )
            support_files_by_camera = discover_invisible_anchor_files(
                target_files,
                camera_map,
                risky_cameras,
            )
            invisible_anchor_support["candidate_files_by_camera"] = {
                camera_name: [str(path) for path in paths]
                for camera_name, paths in support_files_by_camera.items()
            }
            raw_support_matches = build_invisible_anchor_matches(
                support_files_by_camera,
                references=references,
                master_reference=master_reference,
                reference_start=reference_start,
                ignore_metadata=ignore_metadata,
                selected_match_count=len(selected_matches),
            )
            accepted_support_matches = keep_useful_invisible_anchor_matches(
                selected_matches,
                raw_support_matches,
                master_reference,
            )
            if accepted_support_matches:
                matches.extend(accepted_support_matches)
                invisible_anchor_support["used"] = True
                invisible_anchor_support["accepted_count"] = len(accepted_support_matches)
                accepted_by_camera: dict[str, list[str]] = {}
                for support_match in accepted_support_matches:
                    accepted_by_camera.setdefault(
                        support_match.clip.camera_name,
                        [],
                    ).append(str(support_match.clip.path))
                invisible_anchor_support["accepted_files_by_camera"] = accepted_by_camera
        else:
            logger.info("Apoio invisivel nao necessario: todas as cameras tem ancora elegivel.")

    visible_matches = [match for match in matches if not match.clip.is_invisible_anchor]
    support_matches = [match for match in matches if match.clip.is_invisible_anchor]
    results.setdefault("metadata", {})["invisible_anchor_support"] = invisible_anchor_support
    results["reference"] = str(master_reference.path)
    results["references"] = reference_result_entries(references, master_reference)
    results["metadata"].update(
        {
            "master_reference_path": str(master_reference.path),
            "master_reference_name": master_reference.name,
            "master_reference_track_name": master_reference.track_name,
            "master_reference_start_time": master_reference.absolute_start_time,
            "timeline_anchor_start_time": master_reference.absolute_start_time,
            "audio_cache_dir": str(AUDIO_CACHE_DIR),
            "reference_cache_hit_wav_count": sum(1 for reference in references if reference.cache_hit_wav),
            "reference_cache_hit_features_count": sum(1 for reference in references if reference.cache_hit_features),
            "target_cache_hit_wav_count": sum(1 for match in visible_matches if match.clip.cache_hit_wav),
            "target_cache_hit_features_count": sum(1 for match in visible_matches if match.clip.cache_hit_features),
            "offsets_are_master_unified": True,
            "offset_scale": "master_reference_anchor",
            "reference_match_scope": "all_references_master_unified",
            "master_reference_part_count": len(master_reference_parts),
            "camera_global_offset_seconds": camera_global_offset_seconds,
            "camera_offset_seconds_by_name": camera_offset_seconds_by_name or {},
            "use_camera_clock_model": use_camera_clock_model,
            "camera_post_cut_native_late_threshold_seconds": (
                CAMERA_POST_CUT_NATIVE_LATE_THRESHOLD_SECONDS
            ),
            "camera_post_cut_native_min_previous_duration_seconds": (
                CAMERA_POST_CUT_NATIVE_MIN_PREVIOUS_DURATION_SECONDS
            ),
            "camera_post_cut_native_max_gap_seconds": (
                CAMERA_POST_CUT_NATIVE_MAX_GAP_SECONDS
            ),
            "camera_peer_refine_window_seconds": CAMERA_PEER_REFINE_WINDOW_SECONDS,
            "camera_peer_refine_max_delta_seconds": CAMERA_PEER_REFINE_MAX_DELTA_SECONDS,
            "camera_peer_refine_min_z_score": CAMERA_PEER_REFINE_MIN_Z_SCORE,
            "camera_peer_refine_min_prominence": CAMERA_PEER_REFINE_MIN_PROMINENCE,
            "camera_peer_refine_min_overlap_seconds": CAMERA_PEER_REFINE_MIN_OVERLAP_SECONDS,
            "camera_audio_source": "embedded_mp4",
            "camera_audio_uses_extracted_wav": False,
            "invisible_anchor_cache_hit_wav_count": sum(1 for match in support_matches if match.clip.cache_hit_wav),
            "invisible_anchor_cache_hit_features_count": sum(1 for match in support_matches if match.clip.cache_hit_features),
        }
    )

    all_placements = build_camera_block_placements(
        matches,
        master_reference,
        references=references,
        camera_global_offset_seconds=camera_global_offset_seconds,
        camera_offset_seconds_by_name=camera_offset_seconds_by_name or {},
        use_camera_clock_model=use_camera_clock_model,
    )
    invisible_anchor_placements = [
        placement for placement in all_placements if placement.match.clip.is_invisible_anchor
    ]
    placements = [
        placement for placement in all_placements if not placement.match.clip.is_invisible_anchor
    ]
    if invisible_anchor_placements:
        support_metadata = results.setdefault("metadata", {}).setdefault(
            "invisible_anchor_support",
            {},
        )
        support_metadata["prepared_count"] = len(invisible_anchor_placements)
        support_metadata["prepared_offsets_by_camera"] = {}
        for support_placement in invisible_anchor_placements:
            support_metadata["prepared_offsets_by_camera"].setdefault(
                support_placement.match.clip.camera_name,
                [],
            ).append(
                {
                    "file_name": support_placement.match.clip.path.name,
                    "path": str(support_placement.match.clip.path),
                    "final_offset_seconds": support_placement.final_offset_seconds,
                    "sync_method": support_placement.method,
                    "z_score": support_placement.match.chosen_result.z_score,
                }
            )

    placements, blocked_invisible_anchor_placements = split_unsafe_invisible_anchor_placements(
        placements
    )
    if blocked_invisible_anchor_placements:
        support_metadata = results.setdefault("metadata", {}).setdefault(
            "invisible_anchor_support",
            {},
        )
        blocked_by_camera: dict[str, list[str]] = {}
        blocked_reasons_by_camera: dict[str, list[dict]] = {}
        for blocked_placement in blocked_invisible_anchor_placements:
            blocked_clip = blocked_placement.match.clip
            blocked_by_camera.setdefault(blocked_clip.camera_name, []).append(
                str(blocked_clip.path)
            )
            blocked_reasons_by_camera.setdefault(blocked_clip.camera_name, []).append(
                {
                    "file_name": blocked_clip.path.name,
                    "method": blocked_placement.method,
                    "anchor_file_name": blocked_placement.anchor_match.clip.path.name,
                    "anchor_path": str(blocked_placement.anchor_match.clip.path),
                    "candidate_offset_seconds": blocked_placement.final_offset_seconds,
                    "reason": "sem_refino_local_ou_ponte_confiavel",
                }
            )
            logger.warning(
                "Apoio invisivel bloqueado: %s/%s nao teve confirmacao local segura; clip marcado como falha.",
                blocked_clip.camera_name,
                blocked_clip.path.name,
            )
            register_skipped_clip(
                results,
                blocked_clip,
                reason="invisible_anchor_unverified",
                extra_metadata={
                    "camera_name": blocked_clip.camera_name,
                    "metadata_ignored": ignore_metadata,
                    "chosen_reference_path": str(blocked_placement.match.chosen_reference.path),
                    "chosen_reference_name": blocked_placement.match.chosen_reference.name,
                    "sync_method": blocked_placement.method,
                    "offset_decision_reason": (
                        "ancora_invisivel_sem_confirmacao_segura; "
                        f"metodo_candidato={blocked_placement.method}; "
                        f"ancora={blocked_placement.anchor_match.clip.path.name}"
                    ),
                    "correlation_source": blocked_placement.match.chosen_result.source,
                    "correlation_z_score": blocked_placement.match.chosen_result.z_score,
                    "correlation_prominence_ratio": (
                        blocked_placement.match.chosen_result.prominence_ratio
                    ),
                    "individual_dsp_offset_seconds": (
                        blocked_placement.individual_dsp_offset_seconds
                    ),
                    "camera_native_predicted_offset_seconds": (
                        blocked_placement.camera_native_predicted_offset_seconds
                    ),
                    "camera_clock_model_offset_seconds": (
                        blocked_placement.camera_clock_model_offset_seconds
                    ),
                    "camera_block_anchor_name": (
                        blocked_placement.anchor_match.clip.path.name
                    ),
                    "camera_block_anchor_path": str(blocked_placement.anchor_match.clip.path),
                    "camera_block_anchor_is_invisible": True,
                    "candidate_final_offset_seconds": blocked_placement.final_offset_seconds,
                },
            )
        support_metadata["safety_status"] = "blocked"
        support_metadata["blocked_cameras"] = sorted(blocked_by_camera)
        support_metadata["blocked_files_by_camera"] = blocked_by_camera
        support_metadata["blocked_reasons_by_camera"] = blocked_reasons_by_camera
    else:
        results.setdefault("metadata", {}).setdefault(
            "invisible_anchor_support",
            {},
        ).setdefault("safety_status", "ok")

    invisible_anchor_usage: list[dict] = []
    for placement in placements:
        match = placement.match
        clip = match.clip
        chosen_reference = match.chosen_reference
        chosen_result = match.chosen_result
        window_result = match.window_result

        results["offsets"][clip.key] = placement.final_offset_seconds
        results["metadata"][clip.key] = {
            "duration_seconds": clip.duration_seconds,
            "repaired_duration_seconds": clip.duration_seconds,
            "duration_source": "extracted_wav_samples",
            "extracted_wav_path": str(clip.wav_path),
            "cache_hit_wav": clip.cache_hit_wav,
            "cache_hit_features": clip.cache_hit_features,
            "estimated_start_time": clip.estimated_start_time,
            "estimated_offset_seconds": clip.estimated_offset_seconds,
            "camera_name": clip.camera_name,
            "chosen_reference_path": str(chosen_reference.path),
            "chosen_reference_name": chosen_reference.name,
            "chosen_reference_absolute_start_time": chosen_reference.absolute_start_time,
            "chosen_reference_timeline_offset_seconds": placement.reference_delta_to_master_seconds,
            "master_reference_path": str(master_reference.path),
            "master_reference_name": master_reference.name,
            "master_reference_absolute_start_time": master_reference.absolute_start_time,
            "reference_to_master_delta_seconds": placement.reference_delta_to_master_seconds,
            "sync_method": placement.method,
            "correlation_source": chosen_result.source,
            "correlation_z_score": chosen_result.z_score,
            "correlation_prominence_ratio": chosen_result.prominence_ratio,
            "correlation_low_confidence": chosen_result.low_confidence,
            "raw_correlation_offset_seconds": chosen_result.offset_seconds,
            "individual_dsp_offset_seconds": placement.individual_dsp_offset_seconds,
            "anchor_dsp_offset_seconds": placement.anchor_dsp_offset_seconds,
            "camera_block_base_seconds": placement.camera_block_base_seconds,
            "camera_base_candidate_seconds": placement.camera_base_candidate_seconds,
            "camera_base_deviation_seconds": placement.camera_base_deviation_seconds,
            "camera_native_predicted_offset_seconds": (
                placement.camera_native_predicted_offset_seconds
            ),
            "camera_clock_model_offset_seconds": (
                placement.camera_clock_model_offset_seconds
            ),
            "camera_clock_residual_seconds": placement.camera_clock_residual_seconds,
            "camera_clock_base_seconds": placement.camera_clock_base_seconds,
            "camera_clock_drift_rate": placement.camera_clock_drift_rate,
            "camera_clock_drift_ppm": placement.camera_clock_drift_ppm,
            "camera_clock_inlier_count": placement.camera_clock_inlier_count,
            "camera_clock_candidate_count": placement.camera_clock_candidate_count,
            "camera_clock_model_method": placement.camera_clock_model_method,
            "camera_local_refine_reference_name": (
                None
                if placement.local_refinement is None
                else placement.local_refinement.reference.name
            ),
            "camera_local_refine_reference_path": (
                None
                if placement.local_refinement is None
                else str(placement.local_refinement.reference.path)
            ),
            "camera_local_refine_offset_seconds": (
                None
                if placement.local_refinement is None
                else placement.local_refinement.final_offset_seconds
            ),
            "camera_local_refine_delta_seconds": (
                None
                if placement.local_refinement is None
                else placement.local_refinement.delta_from_prediction_seconds
            ),
            "camera_local_refine_z_score": (
                None
                if placement.local_refinement is None
                else placement.local_refinement.result.z_score
            ),
            "camera_local_refine_prominence_ratio": (
                None
                if placement.local_refinement is None
                else placement.local_refinement.result.prominence_ratio
            ),
            "camera_local_refine_window_seconds": CAMERA_LOCAL_REFINE_WINDOW_SECONDS,
            "camera_local_refine_max_delta_seconds": CAMERA_LOCAL_REFINE_MAX_DELTA_SECONDS,
            "camera_peer_refine_reference_clip": (
                None
                if placement.peer_refinement is None
                else placement.peer_refinement.reference_clip_name
            ),
            "camera_peer_refine_reference_camera": (
                None
                if placement.peer_refinement is None
                else placement.peer_refinement.reference_camera_name
            ),
            "camera_peer_refine_offset_seconds": (
                None
                if placement.peer_refinement is None
                else placement.peer_refinement.final_offset_seconds
            ),
            "camera_peer_refine_delta_seconds": (
                None
                if placement.peer_refinement is None
                else placement.peer_refinement.delta_from_current_seconds
            ),
            "camera_peer_refine_z_score": (
                None
                if placement.peer_refinement is None
                else placement.peer_refinement.result.z_score
            ),
            "camera_peer_refine_prominence_ratio": (
                None
                if placement.peer_refinement is None
                else placement.peer_refinement.result.prominence_ratio
            ),
            "camera_peer_refine_overlap_seconds": (
                None
                if placement.peer_refinement is None
                else placement.peer_refinement.overlap_seconds
            ),
            "offset_decision_reason": placement.offset_decision_reason,
            "pre_chronology_master_offset_seconds": placement.individual_dsp_offset_seconds,
            "premiere_timeline_offset_seconds": placement.final_offset_seconds,
            "offset_sign_convention": (
                "premiere_offset_seconds = reference_to_master_delta_seconds "
                "- raw_correlation_offset_seconds"
            ),
            "camera_block_anchor_path": str(placement.anchor_match.clip.path),
            "camera_block_anchor_name": placement.anchor_match.clip.path.name,
            "camera_block_anchor_z_score": placement.anchor_match.chosen_result.z_score,
            "camera_block_anchor_is_invisible": placement.anchor_match.clip.is_invisible_anchor,
            "camera_block_anchor_prominence_ratio": (
                placement.anchor_match.chosen_result.prominence_ratio
            ),
            "camera_global_offset_seconds": camera_global_offset_seconds,
            "camera_specific_offset_seconds": camera_specific_offset_seconds(
                clip.camera_name,
                camera_offset_seconds_by_name or {},
            ),
            "camera_native_relative_start_seconds": placement.native_relative_start_seconds,
            "camera_anchor_native_relative_start_seconds": (
                placement.anchor_native_relative_start_seconds
            ),
            "camera_native_delta_from_anchor_seconds": (
                placement.native_relative_start_seconds
                - placement.anchor_native_relative_start_seconds
            ),
            "camera_native_gap_from_previous_seconds": (
                placement.native_gap_from_previous_seconds
            ),
            "raw_peak_offset_seconds": chosen_result.offset_seconds,
            "window_peak_offset_seconds": (
                None if window_result is None else window_result.offset_seconds
            ),
            "window_peak_z_score": None if window_result is None else window_result.z_score,
            "window_peak_prominence_ratio": (
                None if window_result is None else window_result.prominence_ratio
            ),
            "metadata_ignored": ignore_metadata,
            "camera_chronology_lock_reason": None,
            "chronology_lock_reason": None,
            "camera_block_sync": True,
            "xml_in_seconds": 0.0,
            "xml_out_seconds": clip.duration_seconds,
            "xml_duration_limit_applied": True,
            "master_unified_offset": True,
        }

        if placement.anchor_match.clip.is_invisible_anchor:
            invisible_anchor_usage.append(
                {
                    "camera_name": clip.camera_name,
                    "target_file_name": clip.path.name,
                    "anchor_file_name": placement.anchor_match.clip.path.name,
                    "anchor_path": str(placement.anchor_match.clip.path),
                    "anchor_z_score": placement.anchor_match.chosen_result.z_score,
                }
            )

        if ignore_metadata:
            logger.info(
                "Offset final unificado: %s | master=%s | ref_match=%s | delta_ref=%.6fs | correlacao %.6fs | final %.6fs",
                clip.path.name,
                master_reference.name,
                chosen_reference.name,
                placement.reference_delta_to_master_seconds,
                chosen_result.offset_seconds,
                placement.final_offset_seconds,
            )
        else:
            logger.info(
                "Offset final por bloco: %s | master=%s | estimado %.6fs | correlacao %.6fs | final %.6fs",
                clip.path.name,
                master_reference.name,
                clip.estimated_offset_seconds,
                chosen_result.offset_seconds,
                placement.final_offset_seconds,
            )

    if invisible_anchor_usage:
        results.setdefault("metadata", {}).setdefault(
            "invisible_anchor_support",
            {},
        )["used_as_anchor"] = invisible_anchor_usage

    return results


def build_camera_block_placements(
    matches: list[ClipSyncMatch],
    master_reference: PreparedReference,
    *,
    references: list[PreparedReference] | None = None,
    camera_global_offset_seconds: float = 0.0,
    camera_offset_seconds_by_name: dict[str, float] | None = None,
    use_camera_clock_model: bool = False,
) -> list[CameraBlockPlacement]:
    placements: list[CameraBlockPlacement] = []
    references = references or [master_reference]
    camera_offset_seconds_by_name = camera_offset_seconds_by_name or {}
    for camera_name, camera_matches in group_matches_by_camera(matches).items():
        camera_adjustment = camera_global_offset_seconds + camera_specific_offset_seconds(
            camera_name,
            camera_offset_seconds_by_name,
        )
        native_timing = build_camera_native_timing(camera_matches)
        (
            anchor_candidate,
            camera_base_seconds,
            consensus_candidates,
            all_anchor_candidates,
        ) = choose_camera_anchor_candidate(
            camera_matches,
            native_timing,
            master_reference,
        )
        anchor_match = anchor_candidate.match
        anchor_offset = anchor_candidate.individual_offset_seconds
        anchor_native_start = native_timing[anchor_match.clip.key]["relative_start"]
        clock_model = fit_camera_clock_model(
            all_anchor_candidates,
            fallback_base_seconds=camera_base_seconds,
        )

        logger.info(
            "Ancora da camera: %s | %s | z=%.2f | base_consenso %.6fs | "
            "clock_base %.6fs | drift %.3f ppm | inliers=%d/%d | offset_individual %.6fs",
            camera_name,
            anchor_match.clip.path.name,
            anchor_match.chosen_result.z_score,
            camera_base_seconds,
            clock_model.base_offset_seconds,
            camera_clock_drift_ppm(clock_model),
            clock_model.inlier_count,
            clock_model.candidate_count,
            anchor_offset,
        )

        previous_final_end: float | None = None
        previous_duration_seconds: float | None = None
        for match in sorted(
            camera_matches,
            key=lambda item: (
                native_timing[item.clip.key]["relative_start"],
                item.clip.path.name.casefold(),
            ),
        ):
            timing = native_timing[match.clip.key]
            native_relative_start = float(timing["relative_start"] or 0.0)
            native_delta_from_anchor = native_relative_start - anchor_native_start
            predicted_offset = camera_base_seconds + native_relative_start + camera_adjustment
            clock_model_offset = (
                camera_clock_model_offset(clock_model, native_relative_start)
                + camera_adjustment
            )
            individual_offset = match_offset_on_master_timeline(match, master_reference)
            base_candidate = individual_offset - native_relative_start
            base_deviation = base_candidate - camera_base_seconds
            clock_residual = individual_offset + camera_adjustment - clock_model_offset
            local_refinement: LocalCameraRefinement | None = None
            use_individual, decision_reason = should_use_individual_camera_offset(
                match,
                individual_offset + camera_adjustment,
                previous_final_end,
            )
            individual_is_camera_base_outlier = (
                abs(base_deviation) > CAMERA_BASE_CONSENSUS_TOLERANCE_SECONDS
            )
            if use_camera_clock_model and is_stable_camera_clock_model(clock_model):
                local_refinement = refine_camera_offset_near_prediction(
                    references,
                    match.target_features,
                    master_reference,
                    predicted_final_offset_seconds=clock_model_offset,
                )
                if is_usable_camera_local_refinement(local_refinement):
                    final_offset = local_refinement.final_offset_seconds
                    method = "camera_clock_local_refine"
                    decision_reason = (
                        f"camera_clock_model:{clock_model.method}; "
                        f"refino_local={local_refinement.reference.name}; "
                        f"delta_refino={local_refinement.delta_from_prediction_seconds:.6f}s; "
                        f"z_refino={local_refinement.result.z_score:.2f}"
                    )
                else:
                    final_offset = clock_model_offset
                    method = "camera_clock_model"
                    if local_refinement is None:
                        local_reason = "sem_refino_local"
                    else:
                        local_reason = (
                            f"refino_rejeitado_delta="
                            f"{local_refinement.delta_from_prediction_seconds:.6f}s;"
                            f"z={local_refinement.result.z_score:.2f}"
                        )
                    decision_reason = (
                        f"camera_clock_model:{clock_model.method}; "
                        f"residuo_individual={clock_residual:.6f}s; {local_reason}"
                    )
                use_native_post_cut, native_post_cut_reason = (
                    should_use_native_post_cut_prediction(
                        final_offset_seconds=final_offset,
                        native_predicted_offset_seconds=predicted_offset,
                        previous_final_end_seconds=previous_final_end,
                        previous_duration_seconds=previous_duration_seconds,
                        native_gap_from_previous_seconds=timing["gap_from_previous"],
                        local_refinement=local_refinement,
                    )
                )
                if use_native_post_cut:
                    old_final_offset = final_offset
                    final_offset = predicted_offset
                    method = "camera_clock_native_post_cut"
                    decision_reason = (
                        f"{decision_reason}; {native_post_cut_reason}; "
                        f"offset_refino={old_final_offset:.6f}s; "
                        f"offset_nativo={predicted_offset:.6f}s"
                    )
                elif (
                    method == "camera_clock_model"
                    and is_usable_post_cut_local_refinement(
                        local_refinement,
                        native_predicted_offset_seconds=predicted_offset,
                        clock_model_offset_seconds=clock_model_offset,
                    )
                ):
                    final_offset = local_refinement.final_offset_seconds
                    method = "camera_clock_post_cut_local_refine"
                    decision_reason = (
                        f"{decision_reason}; {native_post_cut_reason}; "
                        f"post_cut_refino_local_intermediario="
                        f"{local_refinement.reference.name}; "
                        f"delta_refino="
                        f"{local_refinement.delta_from_prediction_seconds:.6f}s; "
                        f"z_refino={local_refinement.result.z_score:.2f}; "
                        f"prom_refino="
                        f"{local_refinement.result.prominence_ratio:.3f}; "
                        f"offset_nativo={predicted_offset:.6f}s; "
                        f"offset_modelo={clock_model_offset:.6f}s"
                    )
                if method == "camera_clock_model":
                    use_short_native_gap, short_native_gap_reason = (
                        should_use_short_clip_native_gap_prediction(
                            match=match,
                            final_offset_seconds=final_offset,
                            native_predicted_offset_seconds=predicted_offset,
                            previous_final_end_seconds=previous_final_end,
                            previous_duration_seconds=previous_duration_seconds,
                            native_gap_from_previous_seconds=timing["gap_from_previous"],
                            local_refinement=local_refinement,
                            clock_residual_seconds=clock_residual,
                        )
                    )
                    if use_short_native_gap:
                        old_final_offset = final_offset
                        final_offset = predicted_offset
                        method = "camera_clock_native_gap_short_clip"
                        decision_reason = (
                            f"{decision_reason}; {short_native_gap_reason}; "
                            f"offset_modelo={old_final_offset:.6f}s; "
                            f"offset_nativo={predicted_offset:.6f}s"
                        )
            elif use_camera_clock_model and individual_is_camera_base_outlier:
                local_refinement = refine_camera_offset_near_prediction(
                    references,
                    match.target_features,
                    master_reference,
                    predicted_final_offset_seconds=predicted_offset,
                )
                if is_usable_camera_local_refinement(local_refinement):
                    final_offset = local_refinement.final_offset_seconds
                    method = "camera_base_local_refine"
                    decision_reason = (
                        f"individual_outlier_vs_camera_base:"
                        f"desvio_base={base_deviation:.6f}s;"
                        f"limite={CAMERA_BASE_CONSENSUS_TOLERANCE_SECONDS:.3f}s; "
                        f"clock_model={clock_model.method}; "
                        f"refino_local={local_refinement.reference.name}; "
                        f"delta_refino={local_refinement.delta_from_prediction_seconds:.6f}s; "
                        f"z_refino={local_refinement.result.z_score:.2f}"
                    )
                else:
                    final_offset = predicted_offset
                    method = "camera_base_native_outlier_fallback"
                    if local_refinement is None:
                        local_reason = "sem_refino_local"
                    else:
                        local_reason = (
                            f"refino_rejeitado_delta="
                            f"{local_refinement.delta_from_prediction_seconds:.6f}s;"
                            f"z={local_refinement.result.z_score:.2f}"
                        )
                    decision_reason = (
                        f"individual_outlier_vs_camera_base:"
                        f"desvio_base={base_deviation:.6f}s;"
                        f"limite={CAMERA_BASE_CONSENSUS_TOLERANCE_SECONDS:.3f}s; "
                        f"clock_model={clock_model.method}; {local_reason}; "
                        f"offset_nativo={predicted_offset:.6f}s"
                    )
            elif use_individual:
                final_offset = individual_offset + camera_adjustment
                method = (
                    "camera_block_anchor_individual_dsp"
                    if match.clip.key == anchor_match.clip.key
                    else "camera_block_individual_dsp"
                )
            elif is_usable_weak_individual_near_prediction(
                match,
                individual_offset + camera_adjustment,
                predicted_offset,
                previous_final_end,
            ):
                final_offset = individual_offset + camera_adjustment
                method = "camera_block_weak_individual_near_prediction"
                decision_reason = (
                    f"weak_individual_near_prediction:"
                    f"delta={final_offset - predicted_offset:.6f}s;"
                    f"z={match.chosen_result.z_score:.2f};"
                    f"prom={match.chosen_result.prominence_ratio:.3f}"
                )
            else:
                final_offset = predicted_offset
                method = (
                    "camera_block_anchor_native_fallback"
                    if match.clip.key == anchor_match.clip.key
                    else "camera_block_native_fallback"
                )

            if method in {
                "camera_clock_model",
                "camera_clock_local_refine",
                "camera_clock_post_cut_local_refine",
                "camera_clock_native_post_cut",
                "camera_clock_native_gap_short_clip",
                "camera_base_local_refine",
                "camera_base_native_outlier_fallback",
            }:
                logger.info(
                    "Usando modelo de relogio: %s | metodo=%s | offset_final %.6fs | "
                    "offset_modelo %.6fs | individual %.6fs | residuo %.6fs",
                    match.clip.path.name,
                    method,
                    final_offset,
                    clock_model_offset,
                    individual_offset + camera_adjustment,
                    clock_residual,
                )
            elif not use_individual:
                logger.warning(
                    "Usando fallback nativo para %s: %s | previsto %.6fs | individual %.6fs",
                    match.clip.path.name,
                    decision_reason,
                    predicted_offset,
                    individual_offset + camera_adjustment,
                )
            elif abs(base_deviation) > CAMERA_BASE_CONSENSUS_TOLERANCE_SECONDS:
                logger.warning(
                    "Match individual fora do consenso da camera: %s | individual %.6fs | "
                    "base %.6fs | consenso %.6fs | desvio %.6fs. Mantendo DSP individual "
                    "porque o clipe e longo/confiavel.",
                    match.clip.path.name,
                    individual_offset + camera_adjustment,
                    base_candidate,
                    camera_base_seconds,
                    base_deviation,
                )
            elif method not in {
                "camera_block_anchor_individual_dsp",
                "camera_block_anchor_native_fallback",
            }:
                logger.info(
                    "Usando DSP individual: %s | ancora=%s | delta_nativo %.6fs | "
                    "previsto %.6fs | individual %.6fs",
                    match.clip.path.name,
                    anchor_match.clip.path.name,
                    native_delta_from_anchor,
                    predicted_offset,
                    individual_offset + camera_adjustment,
                )

            placements.append(
                CameraBlockPlacement(
                    match=match,
                    anchor_match=anchor_match,
                    final_offset_seconds=final_offset,
                    individual_dsp_offset_seconds=individual_offset,
                    anchor_dsp_offset_seconds=anchor_offset,
                    camera_block_base_seconds=camera_base_seconds,
                    camera_base_candidate_seconds=base_candidate,
                    camera_base_deviation_seconds=base_deviation,
                    camera_native_predicted_offset_seconds=predicted_offset,
                    camera_clock_model_offset_seconds=clock_model_offset,
                    camera_clock_residual_seconds=clock_residual,
                    camera_clock_base_seconds=clock_model.base_offset_seconds,
                    camera_clock_drift_rate=clock_model.drift_rate,
                    camera_clock_drift_ppm=camera_clock_drift_ppm(clock_model),
                    camera_clock_inlier_count=clock_model.inlier_count,
                    camera_clock_candidate_count=clock_model.candidate_count,
                    camera_clock_model_method=clock_model.method,
                    local_refinement=local_refinement,
                    peer_refinement=None,
                    offset_decision_reason=decision_reason,
                    reference_delta_to_master_seconds=reference_offset_from_master(
                        match.chosen_reference,
                        master_reference,
                    ),
                    native_relative_start_seconds=native_relative_start,
                    anchor_native_relative_start_seconds=anchor_native_start,
                    native_gap_from_previous_seconds=timing["gap_from_previous"],
                    method=method,
                )
            )
            previous_final_end = final_offset + match.clip.duration_seconds
            previous_duration_seconds = match.clip.duration_seconds

    if use_camera_clock_model:
        placements = apply_peer_camera_refinements(placements, references, master_reference)

    return sorted(
        placements,
        key=lambda placement: (
            placement.match.clip.original_index,
            placement.match.clip.path.name.casefold(),
        ),
    )


def group_matches_by_camera(matches: list[ClipSyncMatch]) -> dict[str, list[ClipSyncMatch]]:
    grouped: dict[str, list[ClipSyncMatch]] = {}
    for match in matches:
        grouped.setdefault(match.clip.camera_name, []).append(match)
    return grouped



def split_unsafe_invisible_anchor_placements(
    placements: list[CameraBlockPlacement],
) -> tuple[list[CameraBlockPlacement], list[CameraBlockPlacement]]:
    grouped: dict[str, list[CameraBlockPlacement]] = {}
    for placement in placements:
        if placement.anchor_match.clip.is_invisible_anchor:
            grouped.setdefault(placement.match.clip.camera_name, []).append(placement)

    unsafe_cameras = {
        camera_name
        for camera_name, camera_placements in grouped.items()
        if any(
            not has_safe_invisible_anchor_confirmation(placement)
            for placement in camera_placements
        )
    }
    if not unsafe_cameras:
        return placements, []

    kept: list[CameraBlockPlacement] = []
    blocked: list[CameraBlockPlacement] = []
    for placement in placements:
        if (
            placement.match.clip.camera_name in unsafe_cameras
            and placement.anchor_match.clip.is_invisible_anchor
        ):
            blocked.append(placement)
        else:
            kept.append(placement)
    return kept, blocked


def has_safe_invisible_anchor_confirmation(placement: CameraBlockPlacement) -> bool:
    if not placement.anchor_match.clip.is_invisible_anchor:
        return True
    if is_usable_camera_local_refinement(placement.local_refinement):
        return True
    if placement.peer_refinement is not None:
        return True
    return "peer_bridge" in placement.method or "peer_refine" in placement.method

def cameras_missing_visible_anchor(
    matches: list[ClipSyncMatch],
    master_reference: PreparedReference,
) -> list[str]:
    missing: list[str] = []
    for camera_name, camera_matches in group_matches_by_camera(matches).items():
        visible_matches = [
            match for match in camera_matches if not match.clip.is_invisible_anchor
        ]
        if not visible_matches:
            continue
        if not camera_has_eligible_anchor(visible_matches, master_reference):
            missing.append(camera_name)
    return missing


def camera_has_eligible_anchor(
    camera_matches: list[ClipSyncMatch],
    master_reference: PreparedReference,
) -> bool:
    try:
        native_timing = build_camera_native_timing(camera_matches)
        candidates = build_camera_anchor_candidates(
            camera_matches,
            native_timing,
            master_reference,
        )
    except ValueError:
        return False
    return any(candidate.eligible_as_anchor for candidate in candidates)


def discover_invisible_anchor_files(
    selected_target_files: list[Path],
    camera_map: dict[str, str],
    risky_camera_names: list[str],
) -> dict[str, list[Path]]:
    if not risky_camera_names:
        return {}

    risky_set = set(risky_camera_names)
    selected_keys = {normalized_path_key(path) for path in selected_target_files}
    selected_by_camera: dict[str, list[Path]] = {}
    for target_file in selected_target_files:
        camera_name = camera_map.get(str(target_file)) or target_file.parent.name or "CAM 01"
        if camera_name in risky_set:
            selected_by_camera.setdefault(camera_name, []).append(target_file)

    support_by_camera: dict[str, list[Path]] = {}
    for camera_name, selected_files in selected_by_camera.items():
        ordered_candidates: list[Path] = []
        seen_candidates: set[str] = set()

        def add_candidate(candidate: Path) -> None:
            candidate_key = normalized_path_key(candidate)
            if candidate_key in selected_keys or candidate_key in seen_candidates:
                return
            seen_candidates.add(candidate_key)
            ordered_candidates.append(candidate)

        for parent in sorted({path.parent for path in selected_files}, key=lambda item: str(item).casefold()):
            try:
                camera_files = [
                    item.resolve()
                    for item in parent.iterdir()
                    if item.is_file()
                    and item.suffix.lower() in VIDEO_EXTENSIONS
                    and not is_ignored_path(item)
                    and not is_proxy_file(item)
                ]
            except OSError as exc:
                logger.warning(
                    "Apoio invisivel ignorado para %s: nao foi possivel ler %s: %s",
                    camera_name,
                    parent,
                    exc,
                )
                continue

            camera_files.sort(key=lambda item: (natural_path_key(item), str(item).casefold()))
            index_by_key = {
                normalized_path_key(path): index for index, path in enumerate(camera_files)
            }
            selected_indices = [
                index_by_key[normalized_path_key(path)]
                for path in selected_files
                if normalized_path_key(path) in index_by_key
            ]
            if not selected_indices:
                continue

            min_selected_index = min(selected_indices)
            max_selected_index = max(selected_indices)
            forward_end = min(
                len(camera_files) - 1,
                max_selected_index + INVISIBLE_ANCHOR_FORWARD_RADIUS,
            )
            forward_indices = list(range(max_selected_index + 1, forward_end + 1))
            for index in forward_indices[:INVISIBLE_ANCHOR_NEAR_FORWARD_COUNT]:
                add_candidate(camera_files[index])

            near_forward_keys = {
                normalized_path_key(camera_files[index])
                for index in forward_indices[:INVISIBLE_ANCHOR_NEAR_FORWARD_COUNT]
            }
            long_forward_indices = sorted(
                [
                    index
                    for index in forward_indices
                    if normalized_path_key(camera_files[index]) not in near_forward_keys
                ],
                key=lambda index: (
                    -safe_file_size(camera_files[index]),
                    index - max_selected_index,
                    natural_path_key(camera_files[index]),
                    str(camera_files[index]).casefold(),
                ),
            )
            for index in long_forward_indices[:INVISIBLE_ANCHOR_LONG_FORWARD_COUNT]:
                add_candidate(camera_files[index])

            backward_start = max(0, min_selected_index - INVISIBLE_ANCHOR_BACKWARD_RADIUS)
            backward_indices = list(range(min_selected_index - 1, backward_start - 1, -1))
            backward_indices.sort(
                key=lambda index: (
                    min_selected_index - index,
                    -safe_file_size(camera_files[index]),
                    natural_path_key(camera_files[index]),
                    str(camera_files[index]).casefold(),
                )
            )
            for index in backward_indices[:INVISIBLE_ANCHOR_BACKWARD_COUNT]:
                add_candidate(camera_files[index])

        support_files = ordered_candidates[:INVISIBLE_ANCHOR_MAX_EXTRA_CLIPS_PER_CAMERA]
        if support_files:
            support_by_camera[camera_name] = sort_targets_for_batch(
                support_files,
                {str(path): camera_name for path in support_files},
                ignore_metadata=True,
            )
            logger.info(
                "Apoio invisivel candidatos: %s | %s",
                camera_name,
                ", ".join(path.name for path in support_by_camera[camera_name]),
            )

    return support_by_camera


def safe_file_size(path: Path) -> int:
    try:
        return int(path.stat().st_size)
    except OSError:
        return 0


def normalized_path_key(path: Path) -> str:
    try:
        return str(path.resolve()).casefold()
    except OSError:
        return str(path).casefold()


def build_invisible_anchor_matches(
    support_files_by_camera: dict[str, list[Path]],
    *,
    references: list[PreparedReference],
    master_reference: PreparedReference,
    reference_start: float | None,
    ignore_metadata: bool,
    selected_match_count: int,
) -> list[ClipSyncMatch]:
    support_files = [
        path for paths in support_files_by_camera.values() for path in paths
    ]
    if not support_files:
        return []

    support_camera_map = {
        str(path): camera_name
        for camera_name, paths in support_files_by_camera.items()
        for path in paths
    }
    sorted_support_files = sort_targets_for_batch(
        support_files,
        support_camera_map,
        ignore_metadata=True,
    )
    support_clips = prepare_target_clips(
        target_files=sorted_support_files,
        reference_start=reference_start,
        camera_map=support_camera_map,
        ignore_metadata=ignore_metadata,
        results=None,
        invisible_anchor=True,
        original_index_start=selected_match_count,
        log_label="apoio invisivel",
    )

    support_matches: list[ClipSyncMatch] = []
    for index, clip in enumerate(support_clips, start=1):
        logger.info(
            "Full Scan apoio invisivel %d/%d: %s | camera=%s | %d referencia(s)",
            index,
            len(support_clips),
            clip.path.name,
            clip.camera_name,
            len(references),
        )
        chosen_reference, chosen_result = find_best_reference_match(
            references,
            clip.features,
            master_reference=master_reference,
            estimated_master_offset_seconds=clip.estimated_offset_seconds,
        )
        if chosen_result.z_score < MIN_FULL_SCAN_Z_SCORE:
            logger.warning(
                "Apoio invisivel rejeitado para %s: z-score %.2f < %.2f.",
                clip.path.name,
                chosen_result.z_score,
                MIN_FULL_SCAN_Z_SCORE,
            )
            continue
        support_matches.append(
            ClipSyncMatch(
                clip=clip,
                chosen_reference=chosen_reference,
                chosen_result=chosen_result,
                window_result=None,
                target_features=clip.features,
                metadata_ignored=ignore_metadata,
            )
        )
    return support_matches


def keep_useful_invisible_anchor_matches(
    selected_matches: list[ClipSyncMatch],
    support_matches: list[ClipSyncMatch],
    master_reference: PreparedReference,
) -> list[ClipSyncMatch]:
    if not support_matches:
        return []

    selected_by_camera = group_matches_by_camera(selected_matches)
    support_by_camera = group_matches_by_camera(support_matches)
    kept: list[ClipSyncMatch] = []
    for camera_name, camera_support_matches in support_by_camera.items():
        combined = [*selected_by_camera.get(camera_name, []), *camera_support_matches]
        if camera_has_eligible_anchor(combined, master_reference):
            kept.extend(camera_support_matches)
            logger.info(
                "Apoio invisivel aceito: %s | %d clipe(s) auxiliar(es).",
                camera_name,
                len(camera_support_matches),
            )
        else:
            logger.warning(
                "Apoio invisivel descartado: %s continuou sem ancora elegivel.",
                camera_name,
            )
    return kept


def should_use_individual_camera_offset(
    match: ClipSyncMatch,
    individual_offset_seconds: float,
    previous_final_end_seconds: float | None,
) -> tuple[bool, str]:
    if match.clip.duration_seconds < MIN_INDIVIDUAL_DSP_DURATION_SECONDS:
        return (
            False,
            f"clipe curto ({match.clip.duration_seconds:.1f}s < "
            f"{MIN_INDIVIDUAL_DSP_DURATION_SECONDS:.1f}s)",
        )

    if match.chosen_result.z_score < MIN_FULL_SCAN_Z_SCORE:
        return (
            False,
            f"z-score baixo ({match.chosen_result.z_score:.2f} < "
            f"{MIN_FULL_SCAN_Z_SCORE:.2f})",
        )

    if (
        previous_final_end_seconds is not None
        and individual_offset_seconds
        < previous_final_end_seconds - CAMERA_ORDER_OVERLAP_TOLERANCE_SECONDS
    ):
        overlap = previous_final_end_seconds - individual_offset_seconds
        return (
            False,
            f"violaria ordem da camera (sobreposicao {overlap:.3f}s)",
        )

    return True, "individual_dsp_confiavel"


def is_usable_weak_individual_near_prediction(
    match: ClipSyncMatch,
    individual_offset_seconds: float,
    predicted_offset_seconds: float,
    previous_final_end_seconds: float | None,
) -> bool:
    if match.clip.duration_seconds < MIN_INDIVIDUAL_DSP_DURATION_SECONDS:
        return False
    if match.chosen_result.z_score < CAMERA_WEAK_INDIVIDUAL_MIN_Z_SCORE:
        return False
    if match.chosen_result.prominence_ratio < CAMERA_WEAK_INDIVIDUAL_MIN_PROMINENCE:
        return False
    if abs(individual_offset_seconds - predicted_offset_seconds) > CAMERA_WEAK_INDIVIDUAL_MAX_DELTA_SECONDS:
        return False
    if (
        previous_final_end_seconds is not None
        and individual_offset_seconds
        < previous_final_end_seconds - CAMERA_ORDER_OVERLAP_TOLERANCE_SECONDS
    ):
        return False
    return True

def camera_specific_offset_seconds(
    camera_name: str,
    camera_offset_seconds_by_name: dict[str, float],
) -> float:
    if not camera_offset_seconds_by_name:
        return 0.0

    normalized_camera = camera_name.casefold()
    for raw_name, offset_seconds in camera_offset_seconds_by_name.items():
        normalized_key = raw_name.casefold().strip()
        if not normalized_key:
            continue
        if normalized_camera == normalized_key or normalized_key in normalized_camera:
            return float(offset_seconds)
    return 0.0


def build_camera_native_timing(
    camera_matches: list[ClipSyncMatch],
) -> dict[str, dict[str, float | None]]:
    ordered_matches = sorted(
        camera_matches,
        key=lambda match: (
            camera_recording_start_time(match.clip),
            natural_path_key(match.clip.path),
            match.clip.original_index,
        ),
    )

    timing: dict[str, dict[str, float | None]] = {}
    previous_match: ClipSyncMatch | None = None
    previous_relative_start = 0.0
    for match in ordered_matches:
        native_start = camera_recording_start_time(match.clip)
        if previous_match is None:
            relative_start = 0.0
            gap_from_previous = None
        else:
            previous_native_end = (
                camera_recording_start_time(previous_match.clip)
                + previous_match.clip.duration_seconds
            )
            raw_gap = native_start - previous_native_end
            gap_from_previous = max(0.0, raw_gap)
            if raw_gap < 0:
                logger.warning(
                    "Gap negativo por mtime na camera %s: %s apos %s | %.3fs. "
                    "Usando gap 0 para preservar ordem nativa.",
                    match.clip.camera_name,
                    match.clip.path.name,
                    previous_match.clip.path.name,
                    raw_gap,
                )
            relative_start = (
                previous_relative_start
                + previous_match.clip.duration_seconds
                + gap_from_previous
            )

        timing[match.clip.key] = {
            "native_start": native_start,
            "relative_start": relative_start,
            "gap_from_previous": gap_from_previous,
        }
        previous_match = match
        previous_relative_start = relative_start

    return timing


def camera_recording_start_time(clip: PreparedClip) -> float:
    return os.path.getmtime(clip.path) - clip.duration_seconds


def choose_camera_anchor_candidate(
    camera_matches: list[ClipSyncMatch],
    native_timing: dict[str, dict[str, float | None]],
    master_reference: PreparedReference,
) -> tuple[
    CameraAnchorCandidate,
    float,
    list[CameraAnchorCandidate],
    list[CameraAnchorCandidate],
]:
    candidates = build_camera_anchor_candidates(
        camera_matches,
        native_timing,
        master_reference,
    )
    anchor_pool = [candidate for candidate in candidates if candidate.eligible_as_anchor]
    using_metadata_base = False
    if not anchor_pool:
        metadata_base = metadata_camera_base_seconds(candidates)
        if metadata_base is not None:
            anchor_pool = candidates
            consensus_candidates = candidates
            camera_base_seconds = metadata_base
            using_metadata_base = True
        else:
            anchor_pool = candidates

    if not using_metadata_base:
        consensus_candidates = choose_camera_base_consensus(anchor_pool)
        if not consensus_candidates:
            consensus_candidates = anchor_pool

        camera_base_seconds = weighted_median(
            [
                (candidate.base_offset_seconds, candidate.weight)
                for candidate in consensus_candidates
            ]
        )
    anchor_candidate = max(
        consensus_candidates,
        key=lambda candidate: (
            candidate.weight,
            candidate.match.chosen_result.z_score,
            candidate.match.clip.duration_seconds,
            -candidate.match.clip.original_index,
        ),
    )

    for candidate in candidates:
        logger.info(
            "Candidato de base: %s | individual %.6fs | nativo %.6fs | base %.6fs | "
            "z=%.2f | dur=%.1fs | elegivel=%s",
            candidate.match.clip.path.name,
            candidate.individual_offset_seconds,
            candidate.native_relative_start_seconds,
            candidate.base_offset_seconds,
            candidate.match.chosen_result.z_score,
            candidate.match.clip.duration_seconds,
            candidate.eligible_as_anchor,
        )

    return anchor_candidate, camera_base_seconds, consensus_candidates, candidates



def metadata_reference_overlap_seconds(
    match: ClipSyncMatch,
    master_reference: PreparedReference,
) -> float | None:
    estimated_offset = match.clip.estimated_offset_seconds
    if estimated_offset is None:
        return None
    return timeline_overlap_seconds(
        estimated_offset,
        match.clip.duration_seconds,
        0.0,
        master_reference.duration_seconds,
    )


def metadata_camera_base_seconds(
    candidates: list[CameraAnchorCandidate],
) -> float | None:
    values: list[tuple[float, float]] = []
    for candidate in candidates:
        estimated_offset = candidate.match.clip.estimated_offset_seconds
        if estimated_offset is None:
            continue
        duration_weight = max(1.0, min(candidate.match.clip.duration_seconds, 600.0) / 60.0)
        values.append(
            (
                estimated_offset - candidate.native_relative_start_seconds,
                duration_weight,
            )
        )
    if not values:
        return None
    return weighted_median(values)

def build_camera_anchor_candidates(
    camera_matches: list[ClipSyncMatch],
    native_timing: dict[str, dict[str, float | None]],
    master_reference: PreparedReference,
) -> list[CameraAnchorCandidate]:
    candidates: list[CameraAnchorCandidate] = []
    for match in camera_matches:
        native_relative_start = float(native_timing[match.clip.key]["relative_start"] or 0.0)
        individual_offset = match_offset_on_master_timeline(match, master_reference)
        base_offset = individual_offset - native_relative_start
        duration_factor = max(1.0, min(match.clip.duration_seconds, 600.0) / 60.0)
        confidence_factor = max(match.chosen_result.z_score, 0.0) * max(
            match.chosen_result.prominence_ratio,
            1.0,
        )
        metadata_overlap = metadata_reference_overlap_seconds(match, master_reference)
        dsp_overlap = timeline_overlap_seconds(
            individual_offset,
            match.clip.duration_seconds,
            0.0,
            master_reference.duration_seconds,
        )
        has_plausible_reference_overlap = (
            metadata_overlap is None
            or metadata_overlap >= METADATA_ANCHOR_MIN_REFERENCE_OVERLAP_SECONDS
            or dsp_overlap >= METADATA_ANCHOR_MIN_REFERENCE_OVERLAP_SECONDS
        )
        eligible_as_anchor = (
            match.clip.duration_seconds >= MIN_CAMERA_ANCHOR_DURATION_SECONDS
            and match.chosen_result.z_score >= MIN_FULL_SCAN_Z_SCORE
            and has_plausible_reference_overlap
        )
        candidates.append(
            CameraAnchorCandidate(
                match=match,
                individual_offset_seconds=individual_offset,
                native_relative_start_seconds=native_relative_start,
                base_offset_seconds=base_offset,
                weight=confidence_factor * duration_factor,
                eligible_as_anchor=eligible_as_anchor,
            )
        )

    if not candidates:
        raise ValueError("Nao ha matches suficientes para escolher ancora de camera.")
    return candidates


def choose_camera_base_consensus(
    candidates: list[CameraAnchorCandidate],
) -> list[CameraAnchorCandidate]:
    if len(candidates) <= 2:
        return candidates

    clusters: list[list[CameraAnchorCandidate]] = []
    for seed in candidates:
        cluster = [
            candidate
            for candidate in candidates
            if abs(candidate.base_offset_seconds - seed.base_offset_seconds)
            <= CAMERA_BASE_CONSENSUS_TOLERANCE_SECONDS
        ]
        clusters.append(cluster)

    return max(
        clusters,
        key=lambda cluster: (
            len(cluster),
            sum(candidate.weight for candidate in cluster),
            sum(candidate.match.clip.duration_seconds for candidate in cluster),
            -min(candidate.match.clip.original_index for candidate in cluster),
        ),
    )


def weighted_median(values_and_weights: list[tuple[float, float]]) -> float:
    if not values_and_weights:
        raise ValueError("Nao e possivel calcular mediana ponderada sem valores.")

    ordered = sorted(values_and_weights, key=lambda item: item[0])
    total_weight = sum(max(weight, EPSILON) for _value, weight in ordered)
    midpoint = total_weight / 2.0
    cumulative = 0.0
    for value, weight in ordered:
        cumulative += max(weight, EPSILON)
        if cumulative >= midpoint:
            return float(value)
    return float(ordered[-1][0])


def fit_camera_clock_model(
    candidates: list[CameraAnchorCandidate],
    *,
    fallback_base_seconds: float,
) -> CameraClockModel:
    model_candidates = [
        candidate
        for candidate in candidates
        if candidate.match.clip.duration_seconds >= MIN_INDIVIDUAL_DSP_DURATION_SECONDS
        and candidate.match.chosen_result.z_score >= MIN_FULL_SCAN_Z_SCORE
    ]

    if len(model_candidates) < CAMERA_CLOCK_MIN_POINTS:
        return CameraClockModel(
            base_offset_seconds=fallback_base_seconds,
            drift_rate=1.0,
            inlier_count=len(model_candidates),
            candidate_count=len(model_candidates),
            max_abs_residual_seconds=None,
            median_abs_residual_seconds=None,
            method="fallback_unit_rate_insufficient_points",
        )

    best_inliers = choose_camera_clock_inliers(model_candidates)
    if len(best_inliers) < CAMERA_CLOCK_MIN_POINTS:
        return CameraClockModel(
            base_offset_seconds=fallback_base_seconds,
            drift_rate=1.0,
            inlier_count=len(best_inliers),
            candidate_count=len(model_candidates),
            max_abs_residual_seconds=None,
            median_abs_residual_seconds=None,
            method="fallback_unit_rate_no_stable_inliers",
        )

    _fitted_base_offset, drift_rate = weighted_linear_clock_fit(best_inliers)
    anchor_candidate = min(
        best_inliers,
        key=lambda candidate: (
            candidate.native_relative_start_seconds,
            -candidate.weight,
            candidate.match.clip.original_index,
        ),
    )
    base_offset = (
        anchor_candidate.individual_offset_seconds
        - drift_rate * anchor_candidate.native_relative_start_seconds
    )
    residuals = [
        abs(
            candidate.individual_offset_seconds
            - (base_offset + drift_rate * candidate.native_relative_start_seconds)
        )
        for candidate in best_inliers
    ]

    return CameraClockModel(
        base_offset_seconds=float(base_offset),
        drift_rate=float(drift_rate),
        inlier_count=len(best_inliers),
        candidate_count=len(model_candidates),
        max_abs_residual_seconds=max(residuals) if residuals else None,
        median_abs_residual_seconds=median_value(residuals),
        method="anchored_robust_weighted_linear_fit",
    )


def choose_camera_clock_inliers(
    candidates: list[CameraAnchorCandidate],
) -> list[CameraAnchorCandidate]:
    candidate_sets: list[list[CameraAnchorCandidate]] = []

    for left_index, left in enumerate(candidates):
        for right in candidates[left_index + 1 :]:
            dx = right.native_relative_start_seconds - left.native_relative_start_seconds
            if abs(dx) < CAMERA_CLOCK_MIN_SEPARATION_SECONDS:
                continue
            drift_rate = (
                right.individual_offset_seconds - left.individual_offset_seconds
            ) / dx
            if abs(camera_clock_drift_ppm_from_rate(drift_rate)) > CAMERA_CLOCK_MAX_ABS_DRIFT_PPM:
                continue
            base_offset = left.individual_offset_seconds - (
                drift_rate * left.native_relative_start_seconds
            )
            inliers = [
                candidate
                for candidate in candidates
                if abs(
                    candidate.individual_offset_seconds
                    - (
                        base_offset
                        + drift_rate * candidate.native_relative_start_seconds
                    )
                )
                <= CAMERA_CLOCK_INLIER_TOLERANCE_SECONDS
            ]
            candidate_sets.append(inliers)

    if not candidate_sets:
        return []

    return max(
        candidate_sets,
        key=lambda inliers: (
            len(inliers),
            sum(candidate.weight for candidate in inliers),
            -camera_clock_inlier_median_residual(inliers),
        ),
    )


def camera_clock_inlier_median_residual(
    inliers: list[CameraAnchorCandidate],
) -> float:
    if len(inliers) < CAMERA_CLOCK_MIN_POINTS:
        return float("inf")
    base_offset, drift_rate = weighted_linear_clock_fit(inliers)
    residuals = [
        abs(
            candidate.individual_offset_seconds
            - (base_offset + drift_rate * candidate.native_relative_start_seconds)
        )
        for candidate in inliers
    ]
    median = median_value(residuals)
    return float("inf") if median is None else median


def median_value(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / 2.0


def weighted_linear_clock_fit(
    candidates: list[CameraAnchorCandidate],
) -> tuple[float, float]:
    x_values = np.array(
        [candidate.native_relative_start_seconds for candidate in candidates],
        dtype=np.float64,
    )
    y_values = np.array(
        [candidate.individual_offset_seconds for candidate in candidates],
        dtype=np.float64,
    )
    weights = np.array(
        [max(candidate.weight, EPSILON) for candidate in candidates],
        dtype=np.float64,
    )
    design = np.vstack([np.ones_like(x_values), x_values]).T
    sqrt_weights = np.sqrt(weights)
    base_offset, drift_rate = np.linalg.lstsq(
        design * sqrt_weights[:, None],
        y_values * sqrt_weights,
        rcond=None,
    )[0]
    return float(base_offset), float(drift_rate)


def is_stable_camera_clock_model(model: CameraClockModel) -> bool:
    if model.inlier_count < CAMERA_CLOCK_MIN_POINTS:
        return False
    if not model.method.startswith("anchored_robust"):
        return False
    if (
        model.max_abs_residual_seconds is not None
        and model.max_abs_residual_seconds > CAMERA_CLOCK_INLIER_TOLERANCE_SECONDS
    ):
        return False
    return abs(camera_clock_drift_ppm(model)) <= CAMERA_CLOCK_MAX_ABS_DRIFT_PPM


def camera_clock_model_offset(
    model: CameraClockModel,
    native_relative_start_seconds: float,
) -> float:
    return model.base_offset_seconds + model.drift_rate * native_relative_start_seconds


def camera_clock_drift_ppm(model: CameraClockModel) -> float:
    return camera_clock_drift_ppm_from_rate(model.drift_rate)


def camera_clock_drift_ppm_from_rate(drift_rate: float) -> float:
    return (float(drift_rate) - 1.0) * 1_000_000.0


def refine_camera_offset_near_prediction(
    references: list[PreparedReference],
    target_features: object,
    master_reference: PreparedReference,
    *,
    predicted_final_offset_seconds: float,
    window_seconds: float = CAMERA_LOCAL_REFINE_WINDOW_SECONDS,
) -> LocalCameraRefinement | None:
    candidates: list[LocalCameraRefinement] = []
    for reference in references:
        reference_delta = reference_offset_from_master(reference, master_reference)
        estimated_correlation_offset = reference_delta - predicted_final_offset_seconds
        try:
            result = correlate_feature_envelopes(
                reference.features.normalized_envelope,
                target_features.normalized_envelope,
                feature_rate=reference.features.feature_rate,
                estimated_offset=estimated_correlation_offset,
                window_seconds=window_seconds,
                source="camera_clock_local_refine",
            )
        except (AttributeError, ValueError) as exc:
            logger.debug(
                "Refino local ignorado para %s: %s",
                reference.path.name,
                exc,
            )
            continue

        final_offset = reference_delta - result.offset_seconds
        candidates.append(
            LocalCameraRefinement(
                reference=reference,
                result=result,
                final_offset_seconds=final_offset,
                delta_from_prediction_seconds=(
                    final_offset - predicted_final_offset_seconds
                ),
            )
        )

    if not candidates:
        return None

    return max(candidates, key=camera_local_refinement_score)


def camera_local_refinement_score(refinement: LocalCameraRefinement) -> tuple[float, float, float]:
    delta_penalty = abs(refinement.delta_from_prediction_seconds) * 1.5
    return (
        refinement.result.z_score - delta_penalty,
        refinement.result.prominence_ratio,
        -abs(refinement.delta_from_prediction_seconds),
    )


def is_usable_camera_local_refinement(
    refinement: LocalCameraRefinement | None,
) -> bool:
    if refinement is None:
        return False

    delta_seconds = abs(refinement.delta_from_prediction_seconds)
    if delta_seconds > CAMERA_LOCAL_REFINE_MAX_DELTA_SECONDS:
        return False

    if refinement.result.z_score >= CAMERA_LOCAL_REFINE_MIN_Z_SCORE:
        return True

    if (
        delta_seconds <= CAMERA_LOCAL_REFINE_MEDIUM_MAX_DELTA_SECONDS
        and refinement.result.z_score >= CAMERA_LOCAL_REFINE_MEDIUM_MIN_Z_SCORE
        and refinement.result.prominence_ratio
        >= CAMERA_LOCAL_REFINE_MEDIUM_MIN_PROMINENCE
    ):
        return True

    return (
        delta_seconds <= CAMERA_LOCAL_REFINE_WEAK_MAX_DELTA_SECONDS
        and refinement.result.z_score >= CAMERA_LOCAL_REFINE_WEAK_MIN_Z_SCORE
        and refinement.result.prominence_ratio
        >= CAMERA_LOCAL_REFINE_WEAK_MIN_PROMINENCE
    )


def is_usable_post_cut_local_refinement(
    refinement: LocalCameraRefinement | None,
    *,
    native_predicted_offset_seconds: float,
    clock_model_offset_seconds: float,
) -> bool:
    if refinement is None:
        return False

    delta_seconds = abs(refinement.delta_from_prediction_seconds)
    if delta_seconds > CAMERA_LOCAL_REFINE_MAX_DELTA_SECONDS:
        return False
    if refinement.result.z_score < CAMERA_LOCAL_REFINE_MEDIUM_MIN_Z_SCORE:
        return False
    if refinement.result.prominence_ratio < CAMERA_POST_CUT_LOCAL_MIN_PROMINENCE:
        return False

    lower = min(native_predicted_offset_seconds, clock_model_offset_seconds)
    upper = max(native_predicted_offset_seconds, clock_model_offset_seconds)
    return lower <= refinement.final_offset_seconds <= upper


def should_use_native_post_cut_prediction(
    *,
    final_offset_seconds: float,
    native_predicted_offset_seconds: float,
    previous_final_end_seconds: float | None,
    previous_duration_seconds: float | None,
    native_gap_from_previous_seconds: float | None,
    local_refinement: LocalCameraRefinement | None = None,
) -> tuple[bool, str]:
    if previous_final_end_seconds is None:
        return False, "primeiro_clipe_da_camera"
    if previous_duration_seconds is None:
        return False, "duracao_anterior_indisponivel"
    if previous_duration_seconds < CAMERA_POST_CUT_NATIVE_MIN_PREVIOUS_DURATION_SECONDS:
        return (
            False,
            f"clipe_anterior_curto={previous_duration_seconds:.3f}s",
        )
    if native_gap_from_previous_seconds is None:
        return False, "gap_nativo_indisponivel"
    if native_gap_from_previous_seconds > CAMERA_POST_CUT_NATIVE_MAX_GAP_SECONDS:
        return (
            False,
            "gap_nativo_grande_demais="
            f"{native_gap_from_previous_seconds:.6f}s>"
            f"{CAMERA_POST_CUT_NATIVE_MAX_GAP_SECONDS:.3f}s",
        )

    if (
        local_refinement is not None
        and local_refinement.result.z_score >= CAMERA_LOCAL_REFINE_STRONG_Z_SCORE
        and local_refinement.result.prominence_ratio
        >= CAMERA_LOCAL_REFINE_STRONG_PROMINENCE
    ):
        return (
            False,
            "refino_local_forte_preservado:"
            f"z={local_refinement.result.z_score:.2f};"
            f"prom={local_refinement.result.prominence_ratio:.3f}",
        )

    late_delta = final_offset_seconds - native_predicted_offset_seconds
    if late_delta < CAMERA_POST_CUT_NATIVE_LATE_THRESHOLD_SECONDS:
        return (
            False,
            f"desvio_tardio_pequeno={late_delta:.6f}s",
        )

    native_gap_after_previous = native_predicted_offset_seconds - previous_final_end_seconds
    if native_gap_after_previous < -CAMERA_ORDER_OVERLAP_TOLERANCE_SECONDS:
        return (
            False,
            f"previsao_nativa_sobrepoe_anterior={native_gap_after_previous:.6f}s",
        )

    return (
        True,
        "post_cut_native_prediction:"
        f"desvio_tardio={late_delta:.6f}s;"
        f"gap_nativo={native_gap_from_previous_seconds:.6f}s;"
        f"duracao_anterior={previous_duration_seconds:.3f}s",
    )


def should_use_short_clip_native_gap_prediction(
    *,
    match: ClipSyncMatch,
    final_offset_seconds: float,
    native_predicted_offset_seconds: float,
    previous_final_end_seconds: float | None,
    previous_duration_seconds: float | None,
    native_gap_from_previous_seconds: float | None,
    local_refinement: LocalCameraRefinement | None,
    clock_residual_seconds: float,
) -> tuple[bool, str]:
    if previous_final_end_seconds is None:
        return False, "primeiro_clipe_da_camera"
    if previous_duration_seconds is None:
        return False, "duracao_anterior_indisponivel"
    if previous_duration_seconds < CAMERA_POST_CUT_NATIVE_MIN_PREVIOUS_DURATION_SECONDS:
        return False, f"clipe_anterior_curto={previous_duration_seconds:.3f}s"
    if match.clip.duration_seconds > CAMERA_SHORT_CLIP_NATIVE_MAX_DURATION_SECONDS:
        return False, f"clipe_nao_curto={match.clip.duration_seconds:.3f}s"
    if native_gap_from_previous_seconds is None:
        return False, "gap_nativo_indisponivel"
    if native_gap_from_previous_seconds > CAMERA_SHORT_CLIP_NATIVE_MAX_GAP_SECONDS:
        return (
            False,
            "gap_nativo_curto_grande_demais="
            f"{native_gap_from_previous_seconds:.6f}s>"
            f"{CAMERA_SHORT_CLIP_NATIVE_MAX_GAP_SECONDS:.3f}s",
        )
    if local_refinement is not None and is_usable_camera_local_refinement(local_refinement):
        return False, "refino_local_util_preservado"

    late_delta = final_offset_seconds - native_predicted_offset_seconds
    if late_delta < CAMERA_POST_CUT_NATIVE_LATE_THRESHOLD_SECONDS:
        return False, f"desvio_tardio_pequeno={late_delta:.6f}s"
    if late_delta > CAMERA_SHORT_CLIP_NATIVE_MAX_DELTA_SECONDS:
        return (
            False,
            "desvio_tardio_curto_grande_demais="
            f"{late_delta:.6f}s>{CAMERA_SHORT_CLIP_NATIVE_MAX_DELTA_SECONDS:.3f}s",
        )
    if abs(clock_residual_seconds) < CAMERA_BASE_CONSENSUS_TOLERANCE_SECONDS:
        return False, f"residuo_individual_pequeno={clock_residual_seconds:.6f}s"

    native_gap_after_previous = native_predicted_offset_seconds - previous_final_end_seconds
    if native_gap_after_previous < -CAMERA_ORDER_OVERLAP_TOLERANCE_SECONDS:
        return False, f"previsao_nativa_sobrepoe_anterior={native_gap_after_previous:.6f}s"

    return (
        True,
        "short_clip_native_gap_prediction:"
        f"desvio_tardio={late_delta:.6f}s;"
        f"gap_nativo={native_gap_from_previous_seconds:.6f}s;"
        f"duracao={match.clip.duration_seconds:.3f}s;"
        f"duracao_anterior={previous_duration_seconds:.3f}s;"
        f"residuo_individual={clock_residual_seconds:.6f}s",
    )


def should_attempt_peer_camera_refinement_target(placement: CameraBlockPlacement) -> bool:
    if placement.method in {
        "camera_clock_native_post_cut",
        "camera_clock_native_gap_short_clip",
        "camera_base_native_outlier_fallback",
    }:
        return True

    if placement.method == "camera_clock_local_refine":
        refinement = placement.local_refinement
        if refinement is None:
            return False
        return not (
            refinement.result.z_score >= CAMERA_LOCAL_REFINE_STRONG_Z_SCORE
            and refinement.result.prominence_ratio >= CAMERA_LOCAL_REFINE_STRONG_PROMINENCE
        )

    return False
def apply_peer_camera_refinements(
    placements: list[CameraBlockPlacement],
    references: list[PreparedReference],
    master_reference: PreparedReference,
) -> list[CameraBlockPlacement]:
    updated = list(placements)
    for allow_peer_refined in (False, True):
        for placement_index in sorted(
            range(len(updated)),
            key=lambda index: updated[index].final_offset_seconds,
        ):
            placement = updated[placement_index]
            if not should_attempt_peer_camera_refinement_target(placement):
                continue

            refinement = find_best_peer_camera_refinement(
                placement,
                updated,
                allow_peer_refined=allow_peer_refined,
            )
            if refinement is None:
                continue

            logger.info(
                "Refino camera-camera: %s usando %s | %.6fs -> %.6fs | "
                "delta %.6fs | z=%.2f | overlap %.3fs",
                placement.match.clip.path.name,
                refinement.reference_clip_name,
                placement.final_offset_seconds,
                refinement.final_offset_seconds,
                refinement.delta_from_current_seconds,
                refinement.result.z_score,
                refinement.overlap_seconds,
            )
            updated[placement_index] = replace(
                placement,
                final_offset_seconds=refinement.final_offset_seconds,
                peer_refinement=refinement,
                method="camera_clock_peer_refine",
                offset_decision_reason=(
                    f"{placement.offset_decision_reason}; "
                    "peer_camera_refine:"
                    f"ref={refinement.reference_clip_name};"
                    f"camera={refinement.reference_camera_name};"
                    f"delta={refinement.delta_from_current_seconds:.6f}s;"
                    f"z={refinement.result.z_score:.2f};"
                    f"prom={refinement.result.prominence_ratio:.3f};"
                    f"overlap={refinement.overlap_seconds:.3f}s"
                ),
            )
    updated = apply_peer_camera_previous_gap_backfill(updated)
    updated = apply_peer_camera_bridge_rescue(
        updated,
        references=references,
        master_reference=master_reference,
    )
    return apply_peer_camera_block_bridge_rescue(
        updated,
        references=references,
        master_reference=master_reference,
    )


def apply_peer_camera_previous_gap_backfill(
    placements: list[CameraBlockPlacement],
) -> list[CameraBlockPlacement]:
    updated = list(placements)
    camera_names = sorted({placement.match.clip.camera_name for placement in updated})
    for camera_name in camera_names:
        camera_indices = sorted(
            [
                index
                for index, placement in enumerate(updated)
                if placement.match.clip.camera_name == camera_name
            ],
            key=lambda index: (
                updated[index].native_relative_start_seconds,
                updated[index].match.clip.path.name.casefold(),
            ),
        )
        for position in range(1, len(camera_indices)):
            previous_index = camera_indices[position - 1]
            current_index = camera_indices[position]
            previous = updated[previous_index]
            current = updated[current_index]
            refinement = current.peer_refinement
            if refinement is None:
                continue
            if not is_eligible_peer_gap_backfill_target(previous):
                continue
            native_gap = current.native_gap_from_previous_seconds
            if native_gap is None:
                continue

            inherited_delta = refinement.delta_from_current_seconds
            if abs(inherited_delta) > CAMERA_PEER_REFINE_MAX_DELTA_SECONDS:
                continue

            current_gap = current.final_offset_seconds - (
                previous.final_offset_seconds + previous.match.clip.duration_seconds
            )
            gap_error = current_gap - native_gap
            if (
                abs(gap_error - inherited_delta)
                > CAMERA_PEER_GAP_BACKFILL_TOLERANCE_SECONDS
            ):
                continue

            proposed_offset = previous.final_offset_seconds + inherited_delta
            if not preserves_camera_order_with_offset(previous, proposed_offset, updated):
                continue

            trim = find_peer_gap_direct_trim(
                previous,
                current,
                updated,
                proposed_offset_seconds=proposed_offset,
            )
            trim_reason = ""
            if trim is not None:
                proposed_offset = trim.final_offset_seconds
                trim_reason = (
                    f"; direct_trim_ref={trim.reference_clip_name};"
                    f"direct_delta={trim.delta_from_current_seconds:.6f}s;"
                    f"direct_z={trim.result.z_score:.2f};"
                    f"direct_prom={trim.result.prominence_ratio:.3f}"
                )

            logger.info(
                "Backfill peer-gap: %s herdou ajuste de %s | %.6fs -> %.6fs | "
                "delta %.6fs | gap_atual %.6fs | gap_nativo %.6fs%s",
                previous.match.clip.path.name,
                current.match.clip.path.name,
                previous.final_offset_seconds,
                proposed_offset,
                proposed_offset - previous.final_offset_seconds,
                current_gap,
                native_gap,
                trim_reason,
            )
            updated[previous_index] = replace(
                previous,
                final_offset_seconds=proposed_offset,
                method=(
                    "camera_peer_gap_direct_trim"
                    if trim is not None
                    else "camera_peer_gap_backfill"
                ),
                offset_decision_reason=(
                    f"{previous.offset_decision_reason}; peer_gap_backfill:"
                    f"neighbor={current.match.clip.path.name};"
                    f"delta={inherited_delta:.6f}s;"
                    f"gap_atual={current_gap:.6f}s;"
                    f"gap_nativo={native_gap:.6f}s;"
                    f"z_peer={refinement.result.z_score:.2f};"
                    f"prom_peer={refinement.result.prominence_ratio:.3f}"
                    f"{trim_reason}"
                ),
            )
    return updated


def find_peer_gap_direct_trim(
    placement: CameraBlockPlacement,
    neighbor: CameraBlockPlacement,
    placements: list[CameraBlockPlacement],
    *,
    proposed_offset_seconds: float,
) -> PeerCameraRefinement | None:
    neighbor_refinement = neighbor.peer_refinement
    if neighbor_refinement is None:
        return None

    peer_reference = next(
        (
            item
            for item in placements
            if item.match.clip.camera_name == neighbor_refinement.reference_camera_name
            and item.match.clip.path.name == neighbor_refinement.reference_clip_name
        ),
        None,
    )
    if peer_reference is None:
        return None

    estimated_offset = peer_reference.final_offset_seconds - proposed_offset_seconds
    try:
        result = correlate_feature_envelopes(
            peer_reference.match.target_features.normalized_envelope,
            placement.match.target_features.normalized_envelope,
            feature_rate=peer_reference.match.target_features.feature_rate,
            estimated_offset=estimated_offset,
            window_seconds=CAMERA_PEER_GAP_DIRECT_TRIM_WINDOW_SECONDS,
            source="peer_gap_direct_trim",
        )
    except (AttributeError, ValueError) as exc:
        logger.debug(
            "Trim peer-gap ignorado para %s x %s: %s",
            placement.match.clip.path.name,
            peer_reference.match.clip.path.name,
            exc,
        )
        return None

    final_offset = peer_reference.final_offset_seconds - result.offset_seconds
    delta_from_proposed = final_offset - proposed_offset_seconds
    if abs(delta_from_proposed) > CAMERA_PEER_GAP_DIRECT_TRIM_MAX_DELTA_SECONDS:
        return None
    if result.z_score < CAMERA_PEER_GAP_DIRECT_TRIM_MIN_Z_SCORE:
        return None
    if result.prominence_ratio < CAMERA_PEER_GAP_DIRECT_TRIM_MIN_PROMINENCE:
        return None
    if not preserves_camera_order_with_offset(placement, final_offset, placements):
        return None

    overlap_seconds = timeline_overlap_seconds(
        final_offset,
        placement.match.clip.duration_seconds,
        peer_reference.final_offset_seconds,
        peer_reference.match.clip.duration_seconds,
    )
    return PeerCameraRefinement(
        reference_clip_name=peer_reference.match.clip.path.name,
        reference_camera_name=peer_reference.match.clip.camera_name,
        result=replace(result, source="peer_gap_direct_trim"),
        final_offset_seconds=final_offset,
        delta_from_current_seconds=delta_from_proposed,
        overlap_seconds=overlap_seconds,
    )


def is_eligible_peer_gap_backfill_target(placement: CameraBlockPlacement) -> bool:
    if placement.peer_refinement is not None:
        return False
    if placement.local_refinement is not None and is_usable_camera_local_refinement(
        placement.local_refinement
    ):
        return False
    return placement.method in {
        "camera_clock_native_post_cut",
        "camera_base_native_outlier_fallback",
        "camera_block_native_fallback",
        "camera_block_anchor_native_fallback",
    }


def apply_peer_camera_bridge_rescue(
    placements: list[CameraBlockPlacement],
    *,
    references: list[PreparedReference],
    master_reference: PreparedReference,
) -> list[CameraBlockPlacement]:
    updated = list(placements)
    for placement_index in sorted(
        range(len(updated)),
        key=lambda index: updated[index].final_offset_seconds,
    ):
        placement = updated[placement_index]
        if not should_try_peer_camera_bridge(placement, references, master_reference):
            continue

        refinement = find_best_peer_camera_bridge(
            placement,
            updated,
        )
        if refinement is None:
            continue
        if not preserves_camera_order_with_offset(
            placement,
            refinement.final_offset_seconds,
            updated,
        ):
            continue

        logger.info(
            "Ponte camera-camera: %s usando %s | %.6fs -> %.6fs | "
            "delta %.6fs | z=%.2f | overlap %.3fs",
            placement.match.clip.path.name,
            refinement.reference_clip_name,
            placement.final_offset_seconds,
            refinement.final_offset_seconds,
            refinement.delta_from_current_seconds,
            refinement.result.z_score,
            refinement.overlap_seconds,
        )
        updated[placement_index] = replace(
            placement,
            final_offset_seconds=refinement.final_offset_seconds,
            peer_refinement=refinement,
            method="camera_clock_peer_bridge",
            offset_decision_reason=(
                f"{placement.offset_decision_reason}; "
                "peer_camera_bridge:"
                f"ref={refinement.reference_clip_name};"
                f"camera={refinement.reference_camera_name};"
                f"delta={refinement.delta_from_current_seconds:.6f}s;"
                f"z={refinement.result.z_score:.2f};"
                f"prom={refinement.result.prominence_ratio:.3f};"
                f"overlap={refinement.overlap_seconds:.3f}s"
            ),
        )
    return updated


def should_try_peer_camera_bridge(
    placement: CameraBlockPlacement,
    references: list[PreparedReference],
    master_reference: PreparedReference,
) -> bool:
    if placement.method not in {
        "camera_clock_native_post_cut",
        "camera_base_native_outlier_fallback",
    }:
        return False
    if placement.match.clip.duration_seconds < CAMERA_PEER_BRIDGE_MIN_OVERLAP_SECONDS:
        return False
    coverage_seconds = reference_overlap_for_offset(
        placement.final_offset_seconds,
        placement.match.clip.duration_seconds,
        references,
        master_reference,
    )
    if coverage_seconds >= CAMERA_PEER_BRIDGE_MIN_OVERLAP_SECONDS:
        return False
    return True


def find_best_peer_camera_bridge(
    placement: CameraBlockPlacement,
    placements: list[CameraBlockPlacement],
) -> PeerCameraRefinement | None:
    candidates: list[PeerCameraRefinement] = []
    for peer in placements:
        if peer is placement:
            continue
        if peer.match.clip.camera_name == placement.match.clip.camera_name:
            continue
        if not is_eligible_peer_bridge_reference(peer):
            continue

        try:
            result = correlate_feature_envelopes_full_scan(
                peer.match.target_features.normalized_envelope,
                placement.match.target_features.normalized_envelope,
                feature_rate=peer.match.target_features.feature_rate,
            )
        except (AttributeError, ValueError) as exc:
            logger.debug(
                "Ponte camera-camera ignorada para %s x %s: %s",
                placement.match.clip.path.name,
                peer.match.clip.path.name,
                exc,
            )
            continue

        final_offset = peer.final_offset_seconds - result.offset_seconds
        overlap_seconds = timeline_overlap_seconds(
            final_offset,
            placement.match.clip.duration_seconds,
            peer.final_offset_seconds,
            peer.match.clip.duration_seconds,
        )
        refinement = PeerCameraRefinement(
            reference_clip_name=peer.match.clip.path.name,
            reference_camera_name=peer.match.clip.camera_name,
            result=replace(result, source="peer_camera_full_scan_bridge"),
            final_offset_seconds=final_offset,
            delta_from_current_seconds=final_offset - placement.final_offset_seconds,
            overlap_seconds=overlap_seconds,
        )
        if is_usable_peer_camera_bridge(refinement):
            candidates.append(refinement)

    if not candidates:
        return None
    return max(candidates, key=peer_camera_bridge_score)


def is_eligible_peer_bridge_reference(placement: CameraBlockPlacement) -> bool:
    return is_eligible_peer_reference(placement, allow_peer_refined=True)


def is_usable_peer_camera_bridge(refinement: PeerCameraRefinement) -> bool:
    if refinement.result.z_score < CAMERA_PEER_BRIDGE_MIN_Z_SCORE:
        return False
    if refinement.result.prominence_ratio < CAMERA_PEER_BRIDGE_MIN_PROMINENCE:
        return False
    return refinement.overlap_seconds >= CAMERA_PEER_BRIDGE_MIN_OVERLAP_SECONDS


def peer_camera_bridge_score(refinement: PeerCameraRefinement) -> tuple[float, float, float]:
    return (
        refinement.result.z_score,
        refinement.result.prominence_ratio,
        refinement.overlap_seconds,
    )


def reference_overlap_for_offset(
    offset_seconds: float,
    duration_seconds: float,
    references: list[PreparedReference],
    master_reference: PreparedReference,
) -> float:
    start_seconds = offset_seconds
    end_seconds = offset_seconds + duration_seconds
    best_overlap = 0.0
    for reference in references:
        reference_start = reference_offset_from_master(reference, master_reference)
        reference_end = reference_start + reference.duration_seconds
        best_overlap = max(
            best_overlap,
            min(end_seconds, reference_end) - max(start_seconds, reference_start),
        )
    return max(0.0, best_overlap)


def preserves_camera_order_with_offset(
    placement: CameraBlockPlacement,
    proposed_offset_seconds: float,
    placements: list[CameraBlockPlacement],
) -> bool:
    proposed_end = proposed_offset_seconds + placement.match.clip.duration_seconds
    same_camera = sorted(
        [
            item
            for item in placements
            if item.match.clip.camera_name == placement.match.clip.camera_name
        ],
        key=lambda item: (
            item.native_relative_start_seconds,
            item.match.clip.path.name.casefold(),
        ),
    )
    index = same_camera.index(placement)
    if index > 0:
        previous = same_camera[index - 1]
        previous_end = previous.final_offset_seconds + previous.match.clip.duration_seconds
        if proposed_offset_seconds < previous_end - CAMERA_ORDER_OVERLAP_TOLERANCE_SECONDS:
            return False
    if index + 1 < len(same_camera):
        next_item = same_camera[index + 1]
        if proposed_end > next_item.final_offset_seconds + CAMERA_ORDER_OVERLAP_TOLERANCE_SECONDS:
            return False
    return True


def apply_peer_camera_block_bridge_rescue(
    placements: list[CameraBlockPlacement],
    *,
    references: list[PreparedReference],
    master_reference: PreparedReference,
) -> list[CameraBlockPlacement]:
    updated = list(placements)
    for camera_name in sorted({placement.match.clip.camera_name for placement in updated}):
        camera_placements = [
            placement for placement in updated if placement.match.clip.camera_name == camera_name
        ]
        if len(camera_placements) < 2:
            continue

        bridge = find_best_peer_camera_block_bridge(
            camera_placements,
            updated,
            references=references,
            master_reference=master_reference,
        )
        if bridge is None:
            continue
        if not should_apply_peer_camera_block_bridge(camera_placements, bridge):
            continue

        current_base = camera_block_current_base_seconds(camera_placements)
        metadata_base = metadata_camera_base_from_placements(camera_placements)
        logger.warning(
            "Ponte bloco camera-camera: %s usando %s/%s | base %.6fs -> %.6fs | "
            "delta %.6fs | z=%.2f | overlap %.3fs | metadata_base=%s",
            camera_name,
            bridge.peer.match.clip.camera_name,
            bridge.peer.match.clip.path.name,
            current_base,
            bridge.base_offset_seconds,
            bridge.base_offset_seconds - current_base,
            bridge.result.z_score,
            bridge.overlap_seconds,
            "n/d" if metadata_base is None else f"{metadata_base:.6f}s",
        )
        updated = apply_camera_block_bridge_base(
            updated,
            camera_name,
            bridge,
            references=references,
            master_reference=master_reference,
        )
    return updated


def find_best_peer_camera_block_bridge(
    camera_placements: list[CameraBlockPlacement],
    placements: list[CameraBlockPlacement],
    *,
    references: list[PreparedReference],
    master_reference: PreparedReference,
) -> CameraBlockBridgeCandidate | None:
    candidates: list[CameraBlockBridgeCandidate] = []
    camera_name = camera_placements[0].match.clip.camera_name
    for placement in camera_placements:
        if placement.match.clip.duration_seconds < CAMERA_PEER_BLOCK_BRIDGE_MIN_OVERLAP_SECONDS:
            continue
        for peer in placements:
            if peer.match.clip.camera_name == camera_name:
                continue
            if not is_eligible_peer_block_bridge_reference(
                peer,
                references=references,
                master_reference=master_reference,
            ):
                continue
            try:
                result = correlate_feature_envelopes_full_scan(
                    peer.match.target_features.normalized_envelope,
                    placement.match.target_features.normalized_envelope,
                    feature_rate=peer.match.target_features.feature_rate,
                )
            except (AttributeError, ValueError) as exc:
                logger.debug(
                    "Ponte bloco ignorada para %s x %s: %s",
                    placement.match.clip.path.name,
                    peer.match.clip.path.name,
                    exc,
                )
                continue

            final_offset = peer.final_offset_seconds - result.offset_seconds
            overlap_seconds = timeline_overlap_seconds(
                final_offset,
                placement.match.clip.duration_seconds,
                peer.final_offset_seconds,
                peer.match.clip.duration_seconds,
            )
            candidate = CameraBlockBridgeCandidate(
                placement=placement,
                peer=peer,
                result=replace(result, source="peer_camera_block_bridge"),
                final_offset_seconds=final_offset,
                base_offset_seconds=final_offset - placement.native_relative_start_seconds,
                overlap_seconds=overlap_seconds,
            )
            if is_usable_peer_camera_block_bridge(candidate):
                candidates.append(candidate)

    if not candidates:
        return None

    clusters: list[list[CameraBlockBridgeCandidate]] = []
    for seed in candidates:
        clusters.append(
            [
                candidate
                for candidate in candidates
                if abs(candidate.base_offset_seconds - seed.base_offset_seconds)
                <= CAMERA_PEER_BLOCK_BRIDGE_BASE_TOLERANCE_SECONDS
            ]
        )
    best_cluster = max(
        clusters,
        key=lambda cluster: (
            len(cluster),
            sum(camera_block_bridge_candidate_weight(candidate) for candidate in cluster),
            sum(candidate.overlap_seconds for candidate in cluster),
        ),
    )
    best_candidate = max(best_cluster, key=camera_block_bridge_candidate_score)
    if len(best_cluster) == 1 and not is_strong_single_peer_block_bridge(best_candidate):
        return None

    base = weighted_median(
        [
            (candidate.base_offset_seconds, camera_block_bridge_candidate_weight(candidate))
            for candidate in best_cluster
        ]
    )
    return replace(best_candidate, base_offset_seconds=base)


def is_eligible_peer_block_bridge_reference(
    placement: CameraBlockPlacement,
    *,
    references: list[PreparedReference],
    master_reference: PreparedReference,
) -> bool:
    if not is_eligible_peer_reference(placement, allow_peer_refined=True):
        return False
    coverage_seconds = reference_overlap_for_offset(
        placement.final_offset_seconds,
        placement.match.clip.duration_seconds,
        references,
        master_reference,
    )
    if coverage_seconds >= CAMERA_PEER_BLOCK_BRIDGE_MIN_OVERLAP_SECONDS:
        return True
    return placement.method in {"camera_clock_peer_refine", "camera_clock_peer_bridge"}


def is_usable_peer_camera_block_bridge(candidate: CameraBlockBridgeCandidate) -> bool:
    if candidate.result.z_score < CAMERA_PEER_BLOCK_BRIDGE_MIN_Z_SCORE:
        return False
    if candidate.result.prominence_ratio < CAMERA_PEER_BLOCK_BRIDGE_MIN_PROMINENCE:
        return False
    return candidate.overlap_seconds >= CAMERA_PEER_BLOCK_BRIDGE_MIN_OVERLAP_SECONDS


def is_strong_single_peer_block_bridge(candidate: CameraBlockBridgeCandidate) -> bool:
    return (
        candidate.result.z_score >= CAMERA_PEER_BLOCK_BRIDGE_MIN_Z_SCORE + 4.0
        and candidate.overlap_seconds >= CAMERA_PEER_BLOCK_BRIDGE_MIN_OVERLAP_SECONDS * 3.0
    )


def camera_block_bridge_candidate_weight(candidate: CameraBlockBridgeCandidate) -> float:
    overlap_factor = max(1.0, min(candidate.overlap_seconds, 600.0) / 30.0)
    return candidate.result.z_score * max(candidate.result.prominence_ratio, 1.0) * overlap_factor


def camera_block_bridge_candidate_score(
    candidate: CameraBlockBridgeCandidate,
) -> tuple[float, float, float]:
    return (
        camera_block_bridge_candidate_weight(candidate),
        candidate.result.z_score,
        candidate.overlap_seconds,
    )


def should_apply_peer_camera_block_bridge(
    camera_placements: list[CameraBlockPlacement],
    bridge: CameraBlockBridgeCandidate,
) -> bool:
    current_base = camera_block_current_base_seconds(camera_placements)
    if abs(bridge.base_offset_seconds - current_base) < CAMERA_PEER_REFINE_MAX_DELTA_SECONDS:
        return False

    metadata_base = metadata_camera_base_from_placements(camera_placements)
    if metadata_base is not None:
        current_error = abs(current_base - metadata_base)
        bridge_error = abs(bridge.base_offset_seconds - metadata_base)
        return (
            bridge_error + CAMERA_PEER_BLOCK_BRIDGE_MIN_METADATA_IMPROVEMENT_SECONDS
            < current_error
        )

    return camera_block_has_large_internal_conflict(camera_placements)


def camera_block_current_base_seconds(camera_placements: list[CameraBlockPlacement]) -> float:
    values = [placement.camera_block_base_seconds for placement in camera_placements]
    return median_value(values) or camera_placements[0].camera_block_base_seconds


def metadata_camera_base_from_placements(
    camera_placements: list[CameraBlockPlacement],
) -> float | None:
    values: list[tuple[float, float]] = []
    for placement in camera_placements:
        estimated_offset = placement.match.clip.estimated_offset_seconds
        if estimated_offset is None:
            continue
        duration_weight = max(1.0, min(placement.match.clip.duration_seconds, 600.0) / 60.0)
        values.append(
            (
                estimated_offset - placement.native_relative_start_seconds,
                duration_weight,
            )
        )
    if not values:
        return None
    return weighted_median(values)


def camera_block_has_large_internal_conflict(
    camera_placements: list[CameraBlockPlacement],
) -> bool:
    deviations = [abs(placement.camera_base_deviation_seconds) for placement in camera_placements]
    if not deviations:
        return False
    return max(deviations) > max(60.0, CAMERA_BASE_CONSENSUS_TOLERANCE_SECONDS * 5.0)


def apply_camera_block_bridge_base(
    placements: list[CameraBlockPlacement],
    camera_name: str,
    bridge: CameraBlockBridgeCandidate,
    *,
    references: list[PreparedReference],
    master_reference: PreparedReference,
) -> list[CameraBlockPlacement]:
    updated: list[CameraBlockPlacement] = []
    for placement in placements:
        if placement.match.clip.camera_name != camera_name:
            updated.append(placement)
            continue

        predicted_offset = bridge.base_offset_seconds + placement.native_relative_start_seconds
        local_refinement = refine_camera_offset_near_prediction(
            references,
            placement.match.target_features,
            master_reference,
            predicted_final_offset_seconds=predicted_offset,
        )
        if is_usable_camera_local_refinement(local_refinement):
            final_offset = local_refinement.final_offset_seconds
            method = "camera_block_peer_bridge_local_refine"
            local_reason = (
                f"; refino_local={local_refinement.reference.name};"
                f"delta_refino={local_refinement.delta_from_prediction_seconds:.6f}s;"
                f"z_refino={local_refinement.result.z_score:.2f}"
            )
        else:
            final_offset = predicted_offset
            method = "camera_block_peer_bridge"
            local_reason = "; sem_refino_local_util"

        peer_refinement = PeerCameraRefinement(
            reference_clip_name=bridge.peer.match.clip.path.name,
            reference_camera_name=bridge.peer.match.clip.camera_name,
            result=bridge.result,
            final_offset_seconds=final_offset,
            delta_from_current_seconds=final_offset - placement.final_offset_seconds,
            overlap_seconds=bridge.overlap_seconds,
        )
        updated.append(
            replace(
                placement,
                final_offset_seconds=final_offset,
                anchor_match=bridge.placement.match,
                camera_block_base_seconds=bridge.base_offset_seconds,
                camera_base_deviation_seconds=(
                    placement.camera_base_candidate_seconds - bridge.base_offset_seconds
                ),
                camera_native_predicted_offset_seconds=predicted_offset,
                camera_clock_model_offset_seconds=predicted_offset,
                camera_clock_residual_seconds=(
                    placement.individual_dsp_offset_seconds - predicted_offset
                ),
                camera_clock_base_seconds=bridge.base_offset_seconds,
                camera_clock_drift_rate=1.0,
                camera_clock_drift_ppm=0.0,
                camera_clock_inlier_count=1,
                camera_clock_candidate_count=placement.camera_clock_candidate_count,
                camera_clock_model_method="peer_camera_block_bridge",
                local_refinement=local_refinement,
                peer_refinement=peer_refinement,
                method=method,
                offset_decision_reason=(
                    f"{placement.offset_decision_reason}; peer_camera_block_bridge:"
                    f"ref={bridge.peer.match.clip.path.name};"
                    f"camera={bridge.peer.match.clip.camera_name};"
                    f"base={bridge.base_offset_seconds:.6f}s;"
                    f"z={bridge.result.z_score:.2f};"
                    f"prom={bridge.result.prominence_ratio:.3f};"
                    f"overlap={bridge.overlap_seconds:.3f}s"
                    f"{local_reason}"
                ),
            )
        )
    return updated

def find_best_peer_camera_refinement(
    placement: CameraBlockPlacement,
    placements: list[CameraBlockPlacement],
    *,
    allow_peer_refined: bool,
) -> PeerCameraRefinement | None:
    candidates: list[PeerCameraRefinement] = []
    for peer in placements:
        if peer is placement:
            continue
        if not is_eligible_peer_reference(peer, allow_peer_refined=allow_peer_refined):
            continue
        if peer.match.clip.camera_name == placement.match.clip.camera_name:
            continue

        overlap_seconds = timeline_overlap_seconds(
            placement.final_offset_seconds,
            placement.match.clip.duration_seconds,
            peer.final_offset_seconds,
            peer.match.clip.duration_seconds,
        )
        if overlap_seconds < CAMERA_PEER_REFINE_MIN_OVERLAP_SECONDS:
            continue

        estimated_offset = peer.final_offset_seconds - placement.final_offset_seconds
        try:
            result = correlate_feature_envelopes(
                peer.match.target_features.normalized_envelope,
                placement.match.target_features.normalized_envelope,
                feature_rate=peer.match.target_features.feature_rate,
                estimated_offset=estimated_offset,
                window_seconds=CAMERA_PEER_REFINE_WINDOW_SECONDS,
                source="peer_camera_local_refine",
            )
        except (AttributeError, ValueError) as exc:
            logger.debug(
                "Refino camera-camera ignorado para %s x %s: %s",
                placement.match.clip.path.name,
                peer.match.clip.path.name,
                exc,
            )
            continue

        final_offset = peer.final_offset_seconds - result.offset_seconds
        delta = final_offset - placement.final_offset_seconds
        refinement = PeerCameraRefinement(
            reference_clip_name=peer.match.clip.path.name,
            reference_camera_name=peer.match.clip.camera_name,
            result=result,
            final_offset_seconds=final_offset,
            delta_from_current_seconds=delta,
            overlap_seconds=overlap_seconds,
        )
        if is_usable_peer_camera_refinement(refinement):
            candidates.append(refinement)

    if not candidates:
        return None

    return max(candidates, key=peer_camera_refinement_score)


def is_eligible_peer_reference(
    placement: CameraBlockPlacement,
    *,
    allow_peer_refined: bool,
) -> bool:
    stable_methods = {
        "camera_clock_local_refine",
        "camera_clock_post_cut_local_refine",
        "camera_clock_model",
        "camera_block_individual_dsp",
        "camera_block_anchor_individual_dsp",
        "camera_block_anchor_native_fallback",
        "camera_base_local_refine",
    }
    if allow_peer_refined:
        stable_methods.add("camera_clock_peer_refine")
        stable_methods.add("camera_clock_peer_bridge")
        stable_methods.add("camera_block_peer_bridge")
        stable_methods.add("camera_block_peer_bridge_local_refine")
        stable_methods.add("camera_block_weak_individual_near_prediction")
        stable_methods.add("camera_peer_gap_backfill")
        stable_methods.add("camera_peer_gap_direct_trim")
    if placement.method not in stable_methods:
        return False
    return placement.match.clip.duration_seconds >= CAMERA_PEER_REFINE_MIN_OVERLAP_SECONDS


def is_usable_peer_camera_refinement(refinement: PeerCameraRefinement) -> bool:
    if abs(refinement.delta_from_current_seconds) > CAMERA_PEER_REFINE_MAX_DELTA_SECONDS:
        return False
    if refinement.result.z_score < CAMERA_PEER_REFINE_MIN_Z_SCORE:
        return False
    if refinement.result.prominence_ratio < CAMERA_PEER_REFINE_MIN_PROMINENCE:
        return False
    return True


def peer_camera_refinement_score(refinement: PeerCameraRefinement) -> tuple[float, float, float]:
    delta_penalty = abs(refinement.delta_from_current_seconds) * 0.75
    return (
        refinement.result.z_score - delta_penalty,
        refinement.result.prominence_ratio,
        refinement.overlap_seconds,
    )


def timeline_overlap_seconds(
    left_start_seconds: float,
    left_duration_seconds: float,
    right_start_seconds: float,
    right_duration_seconds: float,
) -> float:
    left_end = left_start_seconds + left_duration_seconds
    right_end = right_start_seconds + right_duration_seconds
    return max(0.0, min(left_end, right_end) - max(left_start_seconds, right_start_seconds))


def match_offset_on_master_timeline(
    match: ClipSyncMatch,
    master_reference: PreparedReference,
) -> float:
    return reference_offset_from_master(
        match.chosen_reference,
        master_reference,
    ) + correlation_offset_to_premiere_offset(match.chosen_result.offset_seconds)


def prepare_target_clips(
    *,
    target_files: list[Path],
    reference_start: float | None,
    camera_map: dict[str, str],
    ignore_metadata: bool = False,
    results: dict | None = None,
    invisible_anchor: bool = False,
    original_index_start: int = 0,
    log_label: str = "alvo",
) -> list[PreparedClip]:
    clips: list[PreparedClip] = []
    for position, target_file in enumerate(target_files, start=1):
        index = original_index_start + position
        logger.info(
            "Preparando %s %d/%d: %s",
            log_label,
            position,
            len(target_files),
            target_file.name,
        )
        try:
            cached_audio = prepare_cached_audio_features(
                target_file,
                AUDIO_CACHE_DIR,
                label=f"target_{index - 1}_{target_file.stem}",
            )
        except Exception as exc:
            logger.error(
                "Falha ao preparar alvo %s. Clip sera marcado como falha e o lote continua: %s",
                target_file,
                exc,
            )
            if results is not None:
                register_unprepared_target(
                    results,
                    target_file,
                    camera_map=camera_map,
                    original_index=index,
                    reason="target_audio_prepare_failed",
                    error=str(exc),
                    ignore_metadata=ignore_metadata,
                )
            continue
        target_wav = cached_audio.wav_path
        features = cached_audio.features
        if reference_start is None:
            if not ignore_metadata:
                raise ValueError("reference_start e obrigatorio quando metadados estao ativos.")
            estimated_start = None
            estimated_offset = None
        else:
            estimated_start = target_file.stat().st_mtime - features.duration_seconds
            estimated_offset = estimated_start - reference_start
        key = str(target_file)

        clips.append(
            PreparedClip(
                path=target_file,
                key=key,
                wav_path=target_wav,
                duration_seconds=features.duration_seconds,
                features=features,
                estimated_start_time=estimated_start,
                estimated_offset_seconds=estimated_offset,
                camera_name=camera_map.get(key) or target_file.parent.name or "CAM 01",
                original_index=index,
                cache_hit_wav=cached_audio.cache_hit_wav,
                cache_hit_features=cached_audio.cache_hit_features,
                is_invisible_anchor=invisible_anchor,
            )
        )

    return sorted(
        clips,
        key=lambda clip: (
            clip.path.stat().st_mtime,
            clip.path.name.casefold(),
            clip.original_index,
        ),
    )


def correlate_feature_envelopes(
    reference_envelope: np.ndarray,
    target_envelope: np.ndarray,
    *,
    feature_rate: float,
    estimated_offset: float,
    window_seconds: float,
    source: str,
) -> CorrelationResult:
    if estimated_offset is None or window_seconds is None:
        raise ValueError("A correlacao deve permanecer restrita a janela de metadados.")

    correlation = fft_correlate_full(target_envelope, reference_envelope)
    center_index = len(reference_envelope) - 1
    expected_peak = center_index + int(round(estimated_offset * feature_rate))
    window_samples = int(round(window_seconds * feature_rate))
    start_index = max(0, expected_peak - window_samples)
    end_index = min(len(correlation), expected_peak + window_samples + 1)

    if start_index >= end_index:
        raise ValueError("Janela de correlacao invalida.")

    segment = correlation[start_index:end_index]
    local_peak_index = int(np.argmax(segment))
    peak_index = start_index + local_peak_index
    peak_value = float(segment[local_peak_index])

    global_mean = float(np.mean(correlation))
    global_std = float(np.std(correlation))
    z_score = (peak_value - global_mean) / global_std if global_std > EPSILON else 0.0
    percentile_95 = float(np.percentile(correlation, 95))
    prominence_ratio = peak_value / max(abs(percentile_95), EPSILON)
    lag_samples = peak_index - center_index
    offset_seconds = lag_samples / float(feature_rate)

    return CorrelationResult(
        offset_seconds=float(offset_seconds),
        peak_value=peak_value,
        z_score=float(z_score),
        prominence_ratio=float(prominence_ratio),
        low_confidence=not is_confident_peak(z_score, prominence_ratio),
        source=source,
    )


def correlate_feature_envelopes_full_scan(
    reference_envelope: np.ndarray,
    target_envelope: np.ndarray,
    *,
    feature_rate: float,
) -> CorrelationResult:
    """Executa busca total de correlacao sem usar mtime, estimativa ou janela."""
    correlation = fft_correlate_full(target_envelope, reference_envelope)
    center_index = len(reference_envelope) - 1
    return peak_from_correlation(
        correlation=correlation,
        center_index=center_index,
        feature_rate=feature_rate,
        start_index=0,
        end_index=len(correlation),
        source="full_scan_ignore_metadata",
    )


def peak_from_correlation(
    *,
    correlation: np.ndarray,
    center_index: int,
    feature_rate: float,
    start_index: int,
    end_index: int,
    source: str,
) -> CorrelationResult:
    if start_index >= end_index:
        raise ValueError("Janela de correlacao invalida.")

    segment = correlation[start_index:end_index]
    local_peak_index = int(np.argmax(segment))
    peak_index = start_index + local_peak_index
    peak_value = float(segment[local_peak_index])

    global_mean = float(np.mean(correlation))
    global_std = float(np.std(correlation))
    z_score = (peak_value - global_mean) / global_std if global_std > EPSILON else 0.0
    percentile_95 = float(np.percentile(correlation, 95))
    prominence_ratio = peak_value / max(abs(percentile_95), EPSILON)
    lag_samples = peak_index - center_index
    offset_seconds = lag_samples / float(feature_rate)

    return CorrelationResult(
        offset_seconds=float(offset_seconds),
        peak_value=peak_value,
        z_score=float(z_score),
        prominence_ratio=float(prominence_ratio),
        low_confidence=not is_confident_peak(z_score, prominence_ratio),
        source=source,
    )


def is_confident_peak(z_score: float, prominence_ratio: float) -> bool:
    return z_score >= MIN_CONFIDENCE_Z_SCORE and prominence_ratio >= MIN_CONFIDENCE_PROMINENCE


def fft_correlate_full(signal: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    """Correlacao 1D completa via FFT, equivalente a scipy.signal.correlate(..., mode='full')."""
    left = np.asarray(signal, dtype=np.float32)
    right = np.asarray(kernel, dtype=np.float32)
    if left.size == 0 or right.size == 0:
        raise ValueError("Nao e possivel correlacionar envelopes vazios.")

    full_size = left.size + right.size - 1
    fft_size = 1 << (full_size - 1).bit_length()
    spectrum_left = np.fft.rfft(left, fft_size)
    spectrum_right = np.fft.rfft(right[::-1], fft_size)
    return np.fft.irfft(spectrum_left * spectrum_right, fft_size)[:full_size]


def correlation_offset_to_premiere_offset(correlation_offset_seconds: float) -> float:
    """
    Converte o lag bruto da correlacao para a convencao usada pelo XML.

    A correlacao `target x reference` retorna sinal oposto ao que o Premiere
    precisa para posicionar o video na timeline. Exemplo pratico:
    - correlacao retorna -511s;
    - o video deve entrar em +511s na timeline da lapela;
    - assim o frame 0 do video alinha com 08:31 dentro do audio de referencia.
    """
    return -float(correlation_offset_seconds)


def apply_spanning_continuity(
    sync_results: dict,
    tolerance_seconds: float = FILE_SPANNING_TOLERANCE_SECONDS,
) -> dict:
    """
    Cola continuacoes diretas de arquivo quando o DSP deixa micro-gaps.

    A regra e deliberadamente conservadora:
    - mesmo grupo fisico (camera ou track de lapela);
    - numero sequencial direto no nome do arquivo;
    - arquivo anterior longo o bastante para parecer quebra automatica;
    - offset atual perto do fim do arquivo anterior dentro da tolerancia;
    - em camera, DSP atual fraco/inconclusivo. Match forte preserva pausa.

    Quando as tres condicoes batem, o offset do arquivo N vira exatamente
    offset(N-1) + duracao_real(N-1), removendo gaps/overlaps pequenos.
    """
    adjustments: list[dict] = []
    adjustments.extend(apply_camera_spanning_continuity(sync_results, tolerance_seconds))
    adjustments.extend(apply_reference_spanning_continuity(sync_results, tolerance_seconds))

    sync_results["spanning_groups"] = [
        *(sync_results.get("spanning_groups") or []),
        *adjustments,
    ]
    metadata = sync_results.setdefault("metadata", {})
    metadata["spanning_continuity_tolerance_seconds"] = tolerance_seconds
    metadata["spanning_min_previous_duration_seconds"] = (
        FILE_SPANNING_MIN_PREVIOUS_DURATION_SECONDS
    )
    metadata["spanning_low_z_score_threshold"] = FILE_SPANNING_LOW_Z_SCORE_THRESHOLD
    metadata["spanning_continuity_adjustment_count"] = len(adjustments)

    for adjustment in adjustments:
        logger.warning(
            "File spanning aplicado: %s -> %s | gap %.6fs | z=%s | offset %.6fs -> %.6fs",
            Path(adjustment["previous_path"]).name,
            Path(adjustment["current_path"]).name,
            adjustment["gap_seconds"],
            adjustment.get("current_z_score"),
            adjustment["old_offset_seconds"],
            adjustment["new_offset_seconds"],
        )

    return sync_results


def apply_camera_spanning_continuity(
    sync_results: dict,
    tolerance_seconds: float,
) -> list[dict]:
    offsets = sync_results.get("offsets") or {}
    metadata = sync_results.setdefault("metadata", {})
    grouped: dict[str, list[dict]] = {}

    for path_text, raw_offset in offsets.items():
        offset_seconds = sync_offset_seconds(raw_offset)
        if offset_seconds is None:
            continue

        item_metadata = metadata.get(path_text) or {}
        duration_seconds = spanning_first_number(
            item_metadata.get("repaired_duration_seconds"),
            item_metadata.get("duration_seconds"),
        )
        sequence_number = file_spanning_sequence_number(path_text)
        if duration_seconds is None or sequence_number is None:
            continue

        camera_name = str(
            item_metadata.get("camera_name")
            or item_metadata.get("camera")
            or item_metadata.get("device")
            or Path(path_text).parent.name
            or "CAM 01"
        )
        grouped.setdefault(camera_name, []).append(
            {
                "path": path_text,
                "sequence_number": sequence_number,
                "offset_seconds": offset_seconds,
                "duration_seconds": float(duration_seconds),
                "z_score": spanning_first_number(
                    item_metadata.get("correlation_z_score"),
                    item_metadata.get("window_peak_z_score"),
                    item_metadata.get("camera_block_anchor_z_score"),
                ),
                "prevent_spanning": camera_spanning_has_trusted_refinement(item_metadata),
            }
        )

    adjustments: list[dict] = []
    for camera_name, items in grouped.items():
        adjustments.extend(
            stitch_spanning_items(
                items,
                group_name=camera_name,
                group_type="camera",
                tolerance_seconds=tolerance_seconds,
                min_previous_duration_seconds=FILE_SPANNING_MIN_PREVIOUS_DURATION_SECONDS,
                require_low_current_z_score=True,
                low_z_score_threshold=FILE_SPANNING_LOW_Z_SCORE_THRESHOLD,
                set_offset=lambda item, new_offset: set_sync_offset(
                    offsets,
                    item["path"],
                    new_offset,
                ),
                set_metadata=lambda item, adjustment: mark_spanning_metadata(
                    metadata,
                    item["path"],
                    adjustment,
                ),
            )
        )
    return adjustments


def camera_spanning_has_trusted_refinement(item_metadata: dict) -> bool:
    method = str(item_metadata.get("sync_method") or "")
    if item_metadata.get("camera_peer_refine_reference_clip"):
        return True
    if method in {
        "camera_clock_peer_refine",
        "camera_clock_post_cut_local_refine",
        "camera_block_peer_bridge",
        "camera_block_peer_bridge_local_refine",
        "camera_peer_gap_backfill",
        "camera_peer_gap_direct_trim",
    }:
        return True
    local_z = spanning_first_number(item_metadata.get("camera_local_refine_z_score"))
    local_delta = spanning_first_number(item_metadata.get("camera_local_refine_delta_seconds"))
    if local_z is None or local_delta is None:
        return False
    return (
        abs(local_delta) <= CAMERA_LOCAL_REFINE_MEDIUM_MAX_DELTA_SECONDS
        and local_z >= CAMERA_LOCAL_REFINE_MEDIUM_MIN_Z_SCORE
    )

def apply_reference_spanning_continuity(
    sync_results: dict,
    tolerance_seconds: float,
) -> list[dict]:
    references = sync_results.get("references") or []
    if not isinstance(references, list):
        return []

    grouped: dict[str, list[dict]] = {}
    for reference in references:
        if not isinstance(reference, dict):
            continue
        path_text = str(reference.get("path") or "")
        if not path_text:
            continue

        offset_seconds = spanning_first_number(
            reference.get("timeline_offset_seconds"),
            reference.get("offset_seconds"),
            0.0,
        )
        duration_seconds = spanning_first_number(
            reference.get("repaired_duration_seconds"),
            reference.get("duration_seconds"),
        )
        sequence_number = file_spanning_sequence_number(path_text)
        if offset_seconds is None or duration_seconds is None or sequence_number is None:
            continue

        track_name = str(
            reference.get("track_name")
            or reference.get("name")
            or Path(path_text).parent.name
            or "Lapela"
        )
        reference["_spanning_path"] = path_text
        reference["_spanning_sequence_number"] = sequence_number
        reference["_spanning_offset_seconds"] = float(offset_seconds)
        reference["_spanning_duration_seconds"] = float(duration_seconds)
        grouped.setdefault(track_name, []).append(reference)

    adjustments: list[dict] = []
    for track_name, items in grouped.items():
        adjustments.extend(
            stitch_spanning_items(
                items,
                group_name=track_name,
                group_type="reference",
                tolerance_seconds=tolerance_seconds,
                min_previous_duration_seconds=FILE_SPANNING_MIN_PREVIOUS_DURATION_SECONDS,
                require_low_current_z_score=False,
                low_z_score_threshold=FILE_SPANNING_LOW_Z_SCORE_THRESHOLD,
                path_getter=lambda item: item["_spanning_path"],
                sequence_getter=lambda item: item["_spanning_sequence_number"],
                offset_getter=lambda item: item["_spanning_offset_seconds"],
                duration_getter=lambda item: item["_spanning_duration_seconds"],
                set_offset=set_reference_spanning_offset,
                set_metadata=lambda item, adjustment: item.update(
                    {
                        "spanning_continuity_applied": True,
                        "spanning_previous_path": adjustment["previous_path"],
                        "spanning_old_offset_seconds": adjustment["old_offset_seconds"],
                        "spanning_new_offset_seconds": adjustment["new_offset_seconds"],
                        "spanning_gap_seconds": adjustment["gap_seconds"],
                        "spanning_current_z_score": adjustment.get("current_z_score"),
                        "spanning_min_previous_duration_seconds": adjustment[
                            "min_previous_duration_seconds"
                        ],
                        "spanning_low_z_score_threshold": adjustment[
                            "low_z_score_threshold"
                        ],
                    }
                ),
            )
        )

    for reference in references:
        if isinstance(reference, dict):
            for key in (
                "_spanning_path",
                "_spanning_sequence_number",
                "_spanning_offset_seconds",
                "_spanning_duration_seconds",
            ):
                reference.pop(key, None)
    return adjustments


def stitch_spanning_items(
    items: list[dict],
    *,
    group_name: str,
    group_type: str,
    tolerance_seconds: float,
    min_previous_duration_seconds: float,
    require_low_current_z_score: bool,
    low_z_score_threshold: float,
    set_offset,
    set_metadata,
    path_getter=None,
    sequence_getter=None,
    offset_getter=None,
    duration_getter=None,
    z_score_getter=None,
) -> list[dict]:
    path_getter = path_getter or (lambda item: item["path"])
    sequence_getter = sequence_getter or (lambda item: item["sequence_number"])
    offset_getter = offset_getter or (lambda item: item["offset_seconds"])
    duration_getter = duration_getter or (lambda item: item["duration_seconds"])
    z_score_getter = z_score_getter or (lambda item: item.get("z_score"))

    ordered = sorted(
        items,
        key=lambda item: (
            sequence_getter(item),
            natural_path_key(Path(str(path_getter(item)))),
        ),
    )
    adjustments: list[dict] = []
    previous: dict | None = None
    previous_offset: float | None = None
    previous_duration: float | None = None
    previous_sequence: int | None = None

    for item in ordered:
        current_sequence = int(sequence_getter(item))
        current_offset = float(offset_getter(item))
        current_duration = float(duration_getter(item))

        if (
            previous is not None
            and previous_offset is not None
            and previous_duration is not None
            and previous_sequence is not None
            and current_sequence == previous_sequence + 1
        ):
            expected_offset = previous_offset + previous_duration
            gap_seconds = current_offset - expected_offset
            current_z_score = spanning_first_number(z_score_getter(item))
            if bool(item.get("prevent_spanning")):
                can_stitch = False
                skip_reason = "current_has_trusted_refinement"
            else:
                can_stitch, skip_reason = should_apply_file_spanning(
                    previous_duration_seconds=previous_duration,
                    gap_seconds=gap_seconds,
                    current_z_score=current_z_score,
                    tolerance_seconds=tolerance_seconds,
                    min_previous_duration_seconds=min_previous_duration_seconds,
                    require_low_current_z_score=require_low_current_z_score,
                    low_z_score_threshold=low_z_score_threshold,
                )
            if can_stitch:
                old_offset = current_offset
                current_offset = expected_offset
                set_offset(item, current_offset)
                adjustment = {
                    "type": group_type,
                    "group": group_name,
                    "previous_path": str(path_getter(previous)),
                    "current_path": str(path_getter(item)),
                    "previous_sequence": previous_sequence,
                    "current_sequence": current_sequence,
                    "old_offset_seconds": old_offset,
                    "new_offset_seconds": current_offset,
                    "previous_duration_seconds": previous_duration,
                    "gap_seconds": gap_seconds,
                    "current_z_score": current_z_score,
                    "tolerance_seconds": tolerance_seconds,
                    "min_previous_duration_seconds": min_previous_duration_seconds,
                    "low_z_score_threshold": low_z_score_threshold,
                }
                set_metadata(item, adjustment)
                adjustments.append(adjustment)
            else:
                logger.debug(
                    "File spanning ignorado: %s -> %s | %s",
                    Path(str(path_getter(previous))).name,
                    Path(str(path_getter(item))).name,
                    skip_reason,
                )

        previous = item
        previous_offset = current_offset
        previous_duration = current_duration
        previous_sequence = current_sequence

    return adjustments


def should_apply_file_spanning(
    *,
    previous_duration_seconds: float,
    gap_seconds: float,
    current_z_score: float | None,
    tolerance_seconds: float,
    min_previous_duration_seconds: float,
    require_low_current_z_score: bool,
    low_z_score_threshold: float,
) -> tuple[bool, str]:
    if previous_duration_seconds < min_previous_duration_seconds:
        return (
            False,
            f"arquivo anterior curto ({previous_duration_seconds:.3f}s < "
            f"{min_previous_duration_seconds:.3f}s)",
        )

    if abs(gap_seconds) > tolerance_seconds:
        return (
            False,
            f"gap fora da tolerancia ({gap_seconds:.3f}s; limite "
            f"{tolerance_seconds:.3f}s)",
        )

    if require_low_current_z_score:
        if current_z_score is not None and current_z_score >= low_z_score_threshold:
            return (
                False,
                f"DSP forte no arquivo atual (z={current_z_score:.2f} >= "
                f"{low_z_score_threshold:.2f})",
            )
        if current_z_score is None:
            return True, "DSP sem z-score; usando continuidade curta"
        return True, f"DSP fraco no arquivo atual (z={current_z_score:.2f})"

    return True, "continuidade direta com gap curto"


def file_spanning_sequence_number(path_value: str | Path) -> int | None:
    stem = Path(str(path_value)).stem
    recorder_match = re.match(
        r"(?:DJI|MIC|REC)[_-]?0*(\d{1,3})(?:[_-]|$)",
        stem,
        flags=re.IGNORECASE,
    )
    if recorder_match:
        return int(recorder_match.group(1))

    groups = re.findall(r"\d+", stem)
    if not groups:
        return None

    for group in reversed(groups):
        if len(group) >= 3:
            return int(group)
    return int(groups[-1])


def sync_offset_seconds(raw_offset: object) -> float | None:
    if isinstance(raw_offset, dict):
        return spanning_first_number(
            raw_offset.get("offset"),
            raw_offset.get("offset_seconds"),
            raw_offset.get("sync_offset_seconds"),
        )
    return spanning_first_number(raw_offset)


def set_sync_offset(offsets: dict, path_text: str, new_offset_seconds: float) -> None:
    raw_offset = offsets.get(path_text)
    if isinstance(raw_offset, dict):
        raw_offset["offset_seconds"] = new_offset_seconds
        raw_offset["offset"] = new_offset_seconds
        return
    offsets[path_text] = new_offset_seconds


def set_reference_spanning_offset(reference: dict, new_offset_seconds: float) -> None:
    reference["timeline_offset_seconds"] = new_offset_seconds
    reference["offset_seconds"] = new_offset_seconds
    reference["fallback_offset_seconds"] = new_offset_seconds
    reference["_spanning_offset_seconds"] = new_offset_seconds


def mark_spanning_metadata(metadata: dict, path_text: str, adjustment: dict) -> None:
    item_metadata = metadata.setdefault(path_text, {})
    item_metadata.update(
        {
            "spanning_continuity_applied": True,
            "spanning_previous_path": adjustment["previous_path"],
            "spanning_old_offset_seconds": adjustment["old_offset_seconds"],
            "spanning_new_offset_seconds": adjustment["new_offset_seconds"],
            "spanning_gap_seconds": adjustment["gap_seconds"],
            "spanning_current_z_score": adjustment.get("current_z_score"),
            "spanning_min_previous_duration_seconds": adjustment[
                "min_previous_duration_seconds"
            ],
            "spanning_low_z_score_threshold": adjustment[
                "low_z_score_threshold"
            ],
        }
    )


def spanning_first_number(*values: object) -> float | None:
    for value in values:
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def parse_camera_offsets(raw_offsets: list[str]) -> dict[str, float]:
    parsed: dict[str, float] = {}
    for raw_offset in raw_offsets:
        if "=" not in raw_offset:
            raise ValueError(
                f"Offset de camera invalido: {raw_offset}. Use CAMERA=SEGUNDOS."
            )
        camera_name, value = raw_offset.split("=", 1)
        camera_name = camera_name.strip()
        if not camera_name:
            raise ValueError(
                f"Offset de camera invalido: {raw_offset}. O nome da camera esta vazio."
            )
        try:
            parsed[camera_name] = float(value.replace(",", "."))
        except ValueError as exc:
            raise ValueError(
                f"Offset de camera invalido: {raw_offset}. Valor numerico esperado."
            ) from exc
    return parsed


def run_pipeline(args: argparse.Namespace) -> int:
    reference_inputs = resolve_existing_paths(args.reference, "Referencia")
    targets_inputs = resolve_existing_paths(args.targets, "Targets")
    output_xml_path = Path(args.output).expanduser().resolve()
    timebase = "30"
    camera_offset_seconds_by_name = parse_camera_offsets(args.camera_offset)

    os.chdir(PROJECT_ROOT)
    ensure_temp_dir()
    if args.project_config_path:
        logger.info("Config de projeto: %s", args.project_config_path)

    reference_files = resolve_media_files(
        reference_inputs,
        AUDIO_EXTENSIONS,
        "referencia",
        include_filters=args.reference_filter,
    )
    reference_files = deduplicate_reference_files(reference_files)
    target_files = resolve_media_files(
        targets_inputs,
        VIDEO_EXTENSIONS,
        "target",
        include_filters=args.target_filter,
    )
    target_files = sort_targets_chronologically(target_files)
    camera_map = build_camera_map(target_files, targets_inputs)

    logger.info("Referencias encontradas: %d", len(reference_files))
    for index, reference_file in enumerate(reference_files, start=1):
        logger.info("Referencia %02d: %s", index, reference_file)
    logger.info("Targets encontrados: %d", len(target_files))
    logger.info("Timebase XML: %s fps", timebase)
    if args.ignore_metadata:
        logger.warning("Flag --ignore-metadata ativa: mtime nao sera usado para estimar offsets.")
    for camera_name in dict.fromkeys(camera_map.values()):
        count = sum(1 for value in camera_map.values() if value == camera_name)
        logger.info("Camera detectada: %s (%d clipe(s))", camera_name, count)
    for index, target_file in enumerate(target_files, start=1):
        logger.info(
            "Ordem cronologica %03d: %s | mtime=%.3f",
            index,
            target_file.name,
            target_file.stat().st_mtime,
        )

    from backend.xml_generator import create_timeline_xml

    sync_results = sync_multiple_tracks_hybrid(
        reference_files,
        target_files,
        camera_map,
        ignore_metadata=args.ignore_metadata,
        camera_global_offset_seconds=args.camera_global_offset,
        camera_offset_seconds_by_name=camera_offset_seconds_by_name,
        use_camera_clock_model=args.use_camera_clock_model,
        explicit_selection=args.explicit_selection,
    )
    if args.project_config_path:
        sync_results.setdefault("metadata", {})["project_config_path"] = args.project_config_path
    sync_results = apply_spanning_continuity(sync_results)
    track_check_rows = build_sync_audit_rows(sync_results)
    track_check = build_camera_track_check(track_check_rows)
    sync_results.setdefault("metadata", {})["track_check"] = track_check
    log_camera_track_check(track_check)
    audit_csv_path, audit_json_path = write_sync_audit_reports(
        sync_results,
        output_xml_path,
        audit_output=args.audit_output,
    )
    logger.info("Auditoria CSV gerada: %s", audit_csv_path)
    logger.info("Auditoria JSON gerada: %s", audit_json_path)
    generated_xml = create_timeline_xml(
        sync_results,
        output_xml_path,
        timebase=timebase,
        camera_map=camera_map,
    )
    logger.info("XML gerado com sucesso: %s", generated_xml)
    try:
        from backend.audio_layout_report import write_audio_layout_reports

        audio_layout_report = write_audio_layout_reports(generated_xml)
        sync_results.setdefault("metadata", {})["audio_layout_report"] = audio_layout_report
        logger.info(
            "AudioLayout report: %s (%d alerta(s)) | CSV: %s | JSON: %s | TXT: %s",
            audio_layout_report.get("status"),
            int(audio_layout_report.get("issue_count") or 0),
            audio_layout_report.get("csv"),
            audio_layout_report.get("json"),
            audio_layout_report.get("txt"),
        )
    except Exception as exc:
        logger.warning("AudioLayout report falhou: %s", exc)
        sync_results.setdefault("metadata", {})["audio_layout_report"] = {
            "status": "ERRO",
            "issue_count": 1,
            "error": str(exc),
        }
    try:
        cache_cleanup_result = update_cache_cleanup_after_completed_sync()
        sync_results.setdefault("metadata", {})["auto_cache_cleanup"] = cache_cleanup_result
        log_cache_cleanup_result(cache_cleanup_result)
    except Exception as exc:
        logger.warning("Auto cache cleanup falhou, mantendo cache atual: %s", exc)
        sync_results.setdefault("metadata", {})["auto_cache_cleanup"] = {
            "enabled": True,
            "interval_syncs": CACHE_CLEANUP_INTERVAL_SYNCS,
            "cleanup_performed": False,
            "error": str(exc),
        }

    if args.cleanup:
        removed = cleanup_temp_wavs()
        logger.info("Cleanup concluido: %d WAV(s) removido(s) de %s", removed, TEMP_DIR)

    print(build_summary(sync_results, generated_xml))

    final_metadata = sync_results.get("metadata") or {}
    final_track_check = final_metadata.get("track_check") or {}
    final_sync_quality = final_metadata.get("sync_quality") or {}
    failed_count = sum(1 for offset in (sync_results.get("offsets") or {}).values() if offset is None)
    track_overlap_count = int(final_track_check.get("total_overlap_count") or 0)
    sync_guard_count = int(final_sync_quality.get("blocking_issue_count") or 0)
    if sync_guard_count:
        logger.error(
            "SyncGuard bloqueou o resultado: %d alerta(s) de risco alto exigem revisao.",
            sync_guard_count,
        )
    if track_overlap_count:
        logger.error(
            "TrackCheck bloqueou o resultado: %d sobreposicao(oes) detectada(s).",
            track_overlap_count,
        )
    return 2 if failed_count or track_overlap_count or sync_guard_count else 0


def main(argv: list[str] | None = None) -> int:
    args: argparse.Namespace | None = None
    try:
        args = apply_project_config(parse_args(argv))
        configure_logging(verbose=args.verbose)
        return run_pipeline(args)
    except KeyboardInterrupt:
        if not logging.getLogger().handlers:
            configure_logging()
        logger.error("Execucao interrompida pelo usuario.")
        return 130
    except Exception as exc:
        if not logging.getLogger().handlers:
            configure_logging()
        logger.error("%s", exc)
        if args is not None and args.verbose:
            logger.exception("Detalhes do erro")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
