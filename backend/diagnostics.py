"""Diagnostic package creation and optional support upload helpers."""

from __future__ import annotations

import json
import os
import platform
import sys
import urllib.error
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


DIAGNOSTIC_DIR = Path("temp") / "diagnostics"
SUPPORT_CONFIG_FILENAME = "support_config.json"
SUPPORT_CONFIG_ENV_VAR = "WAVESYNC_SUPPORT_CONFIG"
TELEGRAM_API_BASE_URL = "https://api.telegram.org"
TELEGRAM_MAX_DOCUMENT_BYTES = 50 * 1024 * 1024
DEFAULT_MAX_PACKAGE_BYTES = 45 * 1024 * 1024
DEFAULT_MAX_SINGLE_FILE_BYTES = 10 * 1024 * 1024
DIAGNOSTIC_EXTENSIONS = {".csv", ".json", ".log", ".xml"}
LAST_SYNC_SUFFIXES = (".xml", "_audit.csv", "_audit.json")


@dataclass(frozen=True)
class SupportConfig:
    telegram_bot_token: str
    telegram_chat_id: str
    api_base_url: str = TELEGRAM_API_BASE_URL
    caption_prefix: str = "WaveSync diagnostico"


@dataclass(frozen=True)
class DiagnosticPackage:
    path: Path
    included_files: list[str]
    skipped_files: list[str]
    total_input_bytes: int


@dataclass(frozen=True)
class TelegramUploadResult:
    ok: bool
    message_id: int | None
    description: str | None


def support_config_candidate_paths(project_root: str | Path) -> list[Path]:
    root = Path(project_root).resolve()
    candidates: list[Path] = []

    env_config = os.environ.get(SUPPORT_CONFIG_ENV_VAR)
    if env_config:
        candidates.append(Path(env_config).expanduser())

    candidates.append(root / SUPPORT_CONFIG_FILENAME)

    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "WaveSync" / SUPPORT_CONFIG_FILENAME)

    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        candidates.append(Path(local_appdata) / "WaveSync" / SUPPORT_CONFIG_FILENAME)

    unique: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        normalized = str(path).casefold()
        if normalized not in seen:
            seen.add(normalized)
            unique.append(path)
    return unique


def find_support_config_path(project_root: str | Path) -> Path | None:
    for path in support_config_candidate_paths(project_root):
        if path.exists() and path.is_file():
            return path
    return None


def load_support_config(project_root: str | Path) -> SupportConfig:
    config_path = find_support_config_path(project_root)
    if config_path is None:
        searched = "\n".join(
            f"- {path}" for path in support_config_candidate_paths(project_root)
        )
        raise FileNotFoundError(
            "Arquivo de suporte nao encontrado. Locais procurados:\n"
            f"{searched}\n\n"
            "Crie esse arquivo a partir de support_config.example.json."
        )

    with config_path.open("r", encoding="utf-8") as handle:
        raw_config = json.load(handle)
    if not isinstance(raw_config, dict):
        raise ValueError("support_config.json deve conter um objeto JSON.")

    token = str(
        raw_config.get("telegram_bot_token")
        or raw_config.get("bot_token")
        or ""
    ).strip()
    chat_id = str(
        raw_config.get("telegram_chat_id")
        or raw_config.get("chat_id")
        or ""
    ).strip()
    api_base_url = str(raw_config.get("api_base_url") or TELEGRAM_API_BASE_URL).strip()
    caption_prefix = str(raw_config.get("caption_prefix") or "WaveSync diagnostico").strip()

    if not token:
        raise ValueError("support_config.json sem telegram_bot_token.")
    if not chat_id:
        raise ValueError("support_config.json sem telegram_chat_id.")

    return SupportConfig(
        telegram_bot_token=token,
        telegram_chat_id=chat_id,
        api_base_url=api_base_url.rstrip("/"),
        caption_prefix=caption_prefix,
    )


def create_diagnostic_package(
    project_root: str | Path,
    *,
    destination_dir: str | Path | None = None,
    max_package_bytes: int = DEFAULT_MAX_PACKAGE_BYTES,
    max_single_file_bytes: int = DEFAULT_MAX_SINGLE_FILE_BYTES,
) -> DiagnosticPackage:
    root = Path(project_root).resolve()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    destination = (
        Path(destination_dir).resolve()
        if destination_dir is not None
        else root / DIAGNOSTIC_DIR
    )
    destination.mkdir(parents=True, exist_ok=True)
    package_path = destination / f"wavesync_diagnostico_{timestamp}.zip"

    included: list[str] = []
    skipped: list[str] = []
    total_input_bytes = 0
    candidates = list(iter_last_sync_diagnostic_candidates(root))

    with zipfile.ZipFile(package_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file_path in candidates:
            try:
                file_size = file_path.stat().st_size
            except OSError as exc:
                skipped.append(f"{relative_name(root, file_path)} | erro stat: {exc}")
                continue

            relative = relative_name(root, file_path)
            if file_size > max_single_file_bytes:
                skipped.append(f"{relative} | maior que limite por arquivo")
                continue
            if total_input_bytes + file_size > max_package_bytes:
                skipped.append(f"{relative} | pacote atingiu limite de tamanho")
                continue

            archive.write(file_path, relative)
            included.append(relative)
            total_input_bytes += file_size

        summary = build_system_summary(
            root,
            included_files=included,
            skipped_files=skipped,
            total_input_bytes=total_input_bytes,
        )
        archive.writestr("resumo_do_sistema.txt", summary)
        archive.writestr(
            "manifest.json",
            json.dumps(
                {
                    "generated_at": datetime.now().isoformat(timespec="seconds"),
                    "project_root": str(root),
                    "machine_name": get_machine_name(),
                    "mode": "last_sync_only",
                    "included_files": included,
                    "skipped_files": skipped,
                    "total_input_bytes": total_input_bytes,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )

    return DiagnosticPackage(
        path=package_path,
        included_files=included,
        skipped_files=skipped,
        total_input_bytes=total_input_bytes,
    )


def iter_last_sync_diagnostic_candidates(project_root: Path):
    """Return only the latest sync output set plus the matching config and logs."""
    latest_output = find_latest_sync_output(project_root)
    yielded: set[Path] = set()

    if latest_output is not None:
        for path in related_output_files(latest_output):
            if is_allowed_diagnostic_file(path):
                resolved = path.resolve()
                yielded.add(resolved)
                yield path

        config_path = config_path_from_audit(latest_output)
        if config_path is not None and config_path.exists() and config_path.resolve() not in yielded:
            yielded.add(config_path.resolve())
            yield config_path
    else:
        for path in latest_files_from_dir(project_root / "configs", recursive=False, limit=1):
            resolved = path.resolve()
            yielded.add(resolved)
            yield path

    for path in latest_files_from_dir(project_root / "logs", recursive=True, limit=3):
        resolved = path.resolve()
        if resolved not in yielded:
            yielded.add(resolved)
            yield path

    for path in latest_files_from_dir(project_root, recursive=False, limit=3):
        resolved = path.resolve()
        if resolved not in yielded and path.suffix.casefold() == ".log":
            yielded.add(resolved)
            yield path


def find_latest_sync_output(project_root: Path) -> Path | None:
    output_dir = project_root / "output"
    if not output_dir.exists():
        return None

    candidates = [
        path
        for path in output_dir.rglob("*")
        if path.is_file() and path.suffix.casefold() in {".xml", ".json", ".csv"}
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def related_output_files(latest_output: Path) -> list[Path]:
    stem = sync_base_stem(latest_output)
    parent = latest_output.parent
    candidates = [parent / f"{stem}{suffix}" for suffix in LAST_SYNC_SUFFIXES]
    return [path for path in candidates if path.exists() and path.is_file()]


def sync_base_stem(path: Path) -> str:
    name = path.name
    for suffix in ("_audit.json", "_audit.csv", ".xml"):
        if name.casefold().endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


def config_path_from_audit(latest_output: Path) -> Path | None:
    audit_path = latest_output.parent / f"{sync_base_stem(latest_output)}_audit.json"
    if not audit_path.exists():
        return None

    try:
        with audit_path.open("r", encoding="utf-8") as handle:
            audit = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None

    metadata = audit.get("metadata") if isinstance(audit, dict) else None
    if not isinstance(metadata, dict):
        return None

    config_value = (
        metadata.get("project_config_path")
        or metadata.get("project_config")
        or metadata.get("config")
    )
    if not config_value:
        return None

    config_path = Path(str(config_value)).expanduser()
    if config_path.is_absolute():
        return config_path
    return latest_output.parents[2] / config_path if len(latest_output.parents) > 2 else config_path


def latest_files_from_dir(directory: Path, *, recursive: bool, limit: int) -> list[Path]:
    if not directory.exists() or not directory.is_dir():
        return []
    iterator = directory.rglob("*") if recursive else directory.glob("*")
    files = [path for path in iterator if is_allowed_diagnostic_file(path)]
    return sorted(files, key=lambda path: path.stat().st_mtime, reverse=True)[:limit]


def is_allowed_diagnostic_file(path: Path) -> bool:
    if not path.is_file():
        return False
    if path.name.casefold() == SUPPORT_CONFIG_FILENAME.casefold():
        return False
    return path.suffix.casefold() in DIAGNOSTIC_EXTENSIONS


def relative_name(project_root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(project_root).as_posix()
    except ValueError:
        return path.name


def get_machine_name() -> str:
    """Return the Windows device name shown in system settings when available."""
    return (
        platform.node()
        or os.environ.get("COMPUTERNAME")
        or os.environ.get("HOSTNAME")
        or "desconhecido"
    )

def build_system_summary(
    project_root: Path,
    *,
    included_files: list[str],
    skipped_files: list[str],
    total_input_bytes: int,
) -> str:
    lines = [
        "WaveSync - Pacote de Diagnostico",
        "=" * 72,
        f"Gerado em        : {datetime.now().isoformat(timespec='seconds')}",
        "Modo             : ultimo sync apenas",
        f"Nome da maquina : {get_machine_name()}",
        f"Sistema          : {platform.platform()}",
        f"Python           : {sys.version.split()[0]}",
        f"Executavel Python: {sys.executable}",
        f"Pasta WaveSync   : {project_root}",
        f"Arquivos incluidos: {len(included_files)}",
        f"Arquivos ignorados: {len(skipped_files)}",
        f"Tamanho base     : {total_input_bytes / (1024.0**2):.2f} MiB",
        "",
        "Conteudo incluido",
        "-" * 72,
    ]
    lines.extend(f"- {name}" for name in included_files)
    if skipped_files:
        lines.extend(["", "Conteudo ignorado", "-" * 72])
        lines.extend(f"- {name}" for name in skipped_files)
    lines.extend(
        [
            "",
            "Privacidade",
            "-" * 72,
            "Este pacote nao inclui audios, videos ou arquivos de midia bruta.",
            "Ele pode conter nomes de arquivos, caminhos locais, configs, XMLs e auditorias.",
        ]
    )
    return "\n".join(lines) + "\n"


def send_diagnostic_package(
    package_path: str | Path,
    support_config: SupportConfig,
) -> TelegramUploadResult:
    path = Path(package_path)
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"Pacote de diagnostico nao encontrado: {path}")
    if path.stat().st_size > TELEGRAM_MAX_DOCUMENT_BYTES:
        raise ValueError(
            "Pacote maior que o limite do Telegram Bot API. "
            f"Tamanho: {path.stat().st_size / (1024.0**2):.2f} MiB"
        )

    caption = f"{support_config.caption_prefix}: {path.name}"
    fields = {
        "chat_id": support_config.telegram_chat_id,
        "caption": caption[:1024],
    }
    body, content_type = build_multipart_body(
        fields,
        file_field="document",
        file_path=path,
    )
    url = (
        f"{support_config.api_base_url}/bot"
        f"{support_config.telegram_bot_token}/sendDocument"
    )
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": content_type},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            response_data = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Telegram HTTP {exc.code}: {details}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Falha de rede ao enviar diagnostico: {exc.reason}") from exc

    payload = json.loads(response_data)
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram recusou envio: {payload}")

    result = payload.get("result") or {}
    message_id = result.get("message_id") if isinstance(result, dict) else None
    return TelegramUploadResult(
        ok=True,
        message_id=int(message_id) if message_id is not None else None,
        description=payload.get("description"),
    )


def build_multipart_body(
    fields: dict[str, str],
    *,
    file_field: str,
    file_path: Path,
) -> tuple[bytes, str]:
    boundary = f"----WaveSyncDiagnostic{uuid.uuid4().hex}"
    chunks: list[bytes] = []

    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"),
                str(value).encode("utf-8"),
                b"\r\n",
            ]
        )

    filename = file_path.name.replace('"', "_")
    chunks.extend(
        [
            f"--{boundary}\r\n".encode("utf-8"),
            (
                f'Content-Disposition: form-data; name="{file_field}"; '
                f'filename="{filename}"\r\n'
            ).encode("utf-8"),
            b"Content-Type: application/zip\r\n\r\n",
            file_path.read_bytes(),
            b"\r\n",
            f"--{boundary}--\r\n".encode("utf-8"),
        ]
    )
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"