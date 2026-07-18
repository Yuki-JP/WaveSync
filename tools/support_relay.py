"""Local office support relay for WaveSync diagnostics.

Run this on the support machine that owns support_config.json. Other WaveSync
clients can POST diagnostic ZIPs to this relay, and the relay forwards them to
Telegram using the private token stored only on this machine.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import replace
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.diagnostics import (  # noqa: E402
    DEFAULT_MAX_PACKAGE_BYTES,
    SupportConfig,
    load_support_config,
    send_diagnostic_package_to_telegram,
)


INBOX_DIR = PROJECT_ROOT / "temp" / "support_relay_inbox"
LOG_PATH = PROJECT_ROOT / "temp" / "support_relay.log"


def log_line(message: str) -> None:
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {message}"
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass
    try:
        print(line)
    except Exception:
        pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="WaveSync support relay")
    parser.add_argument("--host", default="0.0.0.0", help="Host/IP para escutar")
    parser.add_argument("--port", type=int, default=8765, help="Porta para escutar")
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=DEFAULT_MAX_PACKAGE_BYTES,
        help="Tamanho maximo aceito por upload",
    )
    return parser.parse_args()


def safe_filename(value: str | None) -> str:
    name = Path(value or "").name
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._")
    if not name.lower().endswith(".zip"):
        name = f"{name or 'diagnostico'}.zip"
    return name


class RelayServer(ThreadingHTTPServer):
    support_config: SupportConfig
    max_bytes: int


class RelayHandler(BaseHTTPRequestHandler):
    server: RelayServer

    def do_GET(self) -> None:
        if self.path.rstrip("/") in {"", "/health"}:
            self.send_json(200, {"ok": True, "service": "WaveSync support relay"})
            return
        self.send_json(404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/upload":
            self.send_json(404, {"ok": False, "error": "not_found"})
            return

        length_text = self.headers.get("Content-Length") or "0"
        try:
            length = int(length_text)
        except ValueError:
            self.send_json(400, {"ok": False, "error": "invalid_content_length"})
            return

        if length <= 0:
            self.send_json(400, {"ok": False, "error": "empty_upload"})
            return
        if length > self.server.max_bytes:
            self.send_json(413, {"ok": False, "error": "upload_too_large"})
            return

        INBOX_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = safe_filename(self.headers.get("X-WaveSync-Filename"))
        package_path = INBOX_DIR / f"{timestamp}_{filename}"

        data = self.rfile.read(length)
        package_path.write_bytes(data)

        caption_prefix = self.headers.get("X-WaveSync-Caption-Prefix") or ""
        support_config = self.server.support_config
        if caption_prefix.strip():
            support_config = replace(support_config, caption_prefix=caption_prefix.strip())

        try:
            result = send_diagnostic_package_to_telegram(package_path, support_config)
        except Exception as exc:  # noqa: BLE001 - relay must report operational errors to client.
            self.send_json(502, {"ok": False, "error": str(exc)})
            return

        self.send_json(
            200,
            {
                "ok": True,
                "message_id": result.message_id,
                "description": result.description or "sent_to_telegram",
            },
        )

    def log_message(self, format_text: str, *args: object) -> None:
        log_line(f"{self.address_string()} - {format_text % args}")

    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    args = parse_args()
    try:
        support_config = load_support_config(PROJECT_ROOT)
        if support_config.relay_url:
            raise RuntimeError(
                "Esta maquina esta configurada como cliente relay. "
                "Para rodar o servidor, crie support_config.json privado com telegram_bot_token e telegram_chat_id."
            )

        server = RelayServer((args.host, args.port), RelayHandler)
        server.support_config = support_config
        server.max_bytes = args.max_bytes

        log_line("WaveSync support relay")
        log_line(f"Escutando em: http://{args.host}:{args.port}/upload")
        log_line("Pressione Ctrl+C para parar.")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            log_line("Encerrando relay...")
        finally:
            server.server_close()
        return 0
    except Exception as exc:  # noqa: BLE001 - startup failures must be visible in hidden mode.
        log_line(f"[ERRO] Relay encerrado: {exc}")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
