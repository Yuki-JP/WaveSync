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
TELEGRAM_API_BASE_URL = "https://api.telegram.org"
TELEGRAM_MAX_DOCUMENT_BYTES = 50 * 1024 * 1024
DEFAULT_MAX_PACKAGE_BYTES = 45 * 1024 * 1024
DEFAULT_MAX_SINGLE_FILE_BYTES = 10 * 1024 * 1024
DIAGNOSTIC_EXTENSIONS = {".csv", ".json", ".log", ".xml"}


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


def load_support_config(project_root: str | Path) -> SupportConfig:
    config_path = Path(project_root) / SUPPORT_CONFIG_FILENAME
    if not config_path.exists():
        raise FileNotFoundError(
            f"Arquivo de suporte nao encontrado: {config_path}. "
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
    candidates = list(iter_diagnostic_candidates(root))

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


def iter_diagnostic_candidates(project_root: Path):
    search_roots = [
        (project_root / "logs", True),
        (project_root / "configs", False),
        (project_root / "output", True),
    ]

    for search_root, recursive in search_roots:
        if not search_root.exists() or not search_root.is_dir():
            continue
        iterator = search_root.rglob("*") if recursive else search_root.glob("*")
        for path in iterator:
            if is_allowed_diagnostic_file(path):
                yield path

    for path in project_root.glob("*.log"):
        if is_allowed_diagnostic_file(path):
            yield path


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