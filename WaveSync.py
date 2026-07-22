"""Simple Tkinter desktop UI for WaveSync.

Run:
  python WaveSync.py

The user only chooses audio references, chooses camera videos, and clicks sync.
The selection JSON is generated automatically in the background.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from tkinter import BOTH, END, LEFT, filedialog, messagebox
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from backend.diagnostics import (
    SupportConfig,
    create_diagnostic_package,
    load_support_config,
    send_diagnostic_package,
)


def application_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


PROJECT_ROOT = application_root()
SELECTIONS_DIR = PROJECT_ROOT / "selections"
OUTPUT_DIR = PROJECT_ROOT / "output"

AUDIO_EXTENSIONS = {".wav", ".wave", ".mp3", ".aac", ".m4a", ".flac", ".ogg", ".wma"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".mts"}

PREFERRED_SYNC_PYTHON = (
    Path.home()
    / "AppData"
    / "Local"
    / "Programs"
    / "Python"
    / "Python39"
    / "python.exe"
)


def slugify(text: str) -> str:
    normalized = text.strip().casefold()
    normalized = re.sub(r"[^a-z0-9]+", "_", normalized)
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return normalized or "projeto"


def quote_arg(value: str) -> str:
    if not value or any(char.isspace() for char in value):
        return f'"{value}"'
    return value


def format_command(command: list[str]) -> str:
    return " ".join(quote_arg(item) for item in command)


def path_for_json(path: str | Path) -> str:
    return Path(path).expanduser().resolve().as_posix()


def supported_filetypes(extensions: set[str]) -> list[tuple[str, str]]:
    patterns = " ".join(f"*{extension}" for extension in sorted(extensions))
    return [
        ("Arquivos suportados", patterns),
        ("Todos os arquivos", "*.*"),
    ]


def resolve_sync_python() -> str:
    """Use the Python environment that has the validated DSP dependencies."""
    if getattr(sys, "frozen", False):
        return sys.executable

    env_override = os.environ.get("WAVESYNC_SYNC_PYTHON")
    if env_override and Path(env_override).exists():
        return str(Path(env_override))

    local_venv = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    if local_venv.exists():
        return str(local_venv)

    if PREFERRED_SYNC_PYTHON.exists():
        return str(PREFERRED_SYNC_PYTHON)

    python_from_path = shutil.which("python")
    if python_from_path:
        return python_from_path

    return sys.executable


def build_sync_command(selection: Path, xml_output: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [
            sys.executable,
            "--sync-worker",
            "--selection",
            str(selection),
            "--xml-output",
            xml_output,
        ]

    sync_python = resolve_sync_python()
    return [
        sync_python,
        str(Path(__file__).resolve()),
        "--sync-worker",
        "--selection",
        str(selection),
        "--xml-output",
        xml_output,
    ]


def hidden_subprocess_options() -> dict[str, object]:
    """Prevent a console window from appearing while the sync worker runs."""
    if os.name != "nt":
        return {}

    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "startupinfo": startupinfo,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }


def run_embedded_sync_worker(selection: str, xml_output: str) -> int:
    """Generate config from a selection and run main.py inside this process."""
    from tools.make_config import (
        apply_selection,
        build_config,
        parse_args as parse_make_config_args,
        print_summary,
        write_config,
    )
    import main as sync_main

    make_args = apply_selection(
        parse_make_config_args(
            [
                "--from-selection",
                selection,
                "--xml-output",
                xml_output,
            ]
        )
    )
    config, config_path = build_config(make_args)

    print("[SYNC-FROM-SELECTION] WaveSync", flush=True)
    print(f"[SYNC-FROM-SELECTION] Selection : {selection}", flush=True)
    print(f"[SYNC-FROM-SELECTION] Config    : {config_path}", flush=True)
    print_summary(config, config_path)
    write_config(config, config_path, overwrite=True)
    print("", flush=True)
    print(f"[OK] Config salvo: {config_path}", flush=True)
    print("", flush=True)
    print("=" * 72, flush=True)
    print("SYNC", flush=True)
    print("=" * 72, flush=True)
    print(f"{sys.executable} main.py --config {config_path}", flush=True)
    print("", flush=True)

    return sync_main.main(["--config", str(config_path)])


def parse_cli_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="WaveSync desktop app.")
    parser.add_argument("--sync-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--selection", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--xml-output", default=None, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def safe_group_name(parent: Path, existing: dict[str, list[str]]) -> str:
    base = parent.name.strip() or "grupo"
    candidate = base
    index = 2
    existing_slugs = {slugify(name) for name in existing}
    while slugify(candidate) in existing_slugs and candidate not in existing:
        candidate = f"{base} {index}"
        index += 1
    return candidate


def add_files_grouped_by_parent(
    groups: dict[str, list[str]],
    raw_paths: tuple[str, ...] | list[str],
    *,
    extensions: set[str],
) -> tuple[int, list[str]]:
    added = 0
    warnings: list[str] = []
    seen = {
        str(Path(path).expanduser().resolve()).casefold()
        for paths in groups.values()
        for path in paths
    }

    for raw_path in raw_paths:
        path = Path(str(raw_path)).expanduser()
        if not path.exists() or not path.is_file():
            warnings.append(f"Nao encontrado: {path}")
            continue
        if path.suffix.casefold() not in extensions:
            warnings.append(f"Extensao invalida: {path.name}")
            continue

        lowered = str(path).casefold()
        if extensions == VIDEO_EXTENSIONS and "proxy" in lowered:
            warnings.append(f"Proxy ignorado: {path.name}")
            continue
        if extensions == AUDIO_EXTENSIONS and (
            "drift_corrected" in lowered
        ):
            warnings.append(f"Drift corrected ignorado: {path.name}")
            continue

        resolved_key = str(path.resolve()).casefold()
        if resolved_key in seen:
            warnings.append(f"Duplicado ignorado: {path.name}")
            continue

        group_name = safe_group_name(path.parent, groups)
        groups.setdefault(group_name, []).append(path_for_json(path))
        seen.add(resolved_key)
        added += 1

    return added, warnings


def count_files(groups: dict[str, list[str]]) -> int:
    return sum(len(paths) for paths in groups.values())


def latest_summary_value(text: str, label: str) -> str:
    matches = re.findall(rf"^{re.escape(label)}\s*:\s*(.+?)\s*$", text, flags=re.MULTILINE)
    return matches[-1].strip() if matches else ""


class WaveSyncSimpleApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("WaveSync")
        self.geometry("980x720")
        self.minsize(820, 600)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M")
        self.project_name = tk.StringVar(value=f"sync_{timestamp}")
        self.status = tk.StringVar(value="Escolha os audios e videos para comecar.")
        self.last_xml_output: str | None = None
        self.references: dict[str, list[str]] = {}
        self.targets: dict[str, list[str]] = {}
        self.log_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.running_process: subprocess.Popen[str] | None = None
        self.diagnostic_running = False
        self.collecting_sync_output = False
        self.current_sync_output: list[str] = []

        self._build()
        self.after(100, self.drain_log_queue)

    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        self.rowconfigure(3, weight=1)

        header = ttk.Frame(self, padding=16)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)

        title = ttk.Label(header, text="WaveSync", font=("Segoe UI", 18, "bold"))
        title.grid(row=0, column=0, columnspan=3, sticky="w")
        subtitle = ttk.Label(
            header,
            text="Escolha os arquivos que voce quer sincronizar.")
        subtitle.grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 12))

        ttk.Label(header, text="Nome do projeto").grid(row=2, column=0, sticky="w")
        ttk.Entry(header, textvariable=self.project_name).grid(
            row=2,
            column=1,
            sticky="ew",
            padx=(8, 0),
        )

        actions = ttk.Frame(self, padding=(16, 0, 16, 12))
        actions.grid(row=1, column=0, sticky="ew")
        actions.columnconfigure(0, weight=1)
        actions.columnconfigure(1, weight=1)
        actions.columnconfigure(2, weight=1)

        self.audio_button = ttk.Button(
            actions,
            text="1. Escolher audios de referencia",
            command=self.choose_references,
        )
        self.video_button = ttk.Button(
            actions,
            text="2. Escolher videos das cameras",
            command=self.choose_targets,
        )
        self.sync_button = ttk.Button(
            actions,
            text="3. Sincronizar",
            command=self.run_sync,
        )
        self.diagnostic_button = ttk.Button(
            actions,
            text="Enviar diagnostico para suporte",
            command=self.send_diagnostic,
        )

        self.audio_button.grid(row=0, column=0, sticky="ew", padx=(0, 8), ipady=8)
        self.video_button.grid(row=0, column=1, sticky="ew", padx=8, ipady=8)
        self.sync_button.grid(row=0, column=2, sticky="ew", padx=(8, 0), ipady=8)
        self.diagnostic_button.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(10, 0), ipady=6)

        summary = ttk.LabelFrame(self, text="Arquivos escolhidos", padding=10)
        summary.grid(row=2, column=0, sticky="nsew", padx=16, pady=(0, 8))
        summary.columnconfigure(0, weight=1)
        summary.columnconfigure(1, weight=1)
        summary.rowconfigure(0, weight=1)

        self.reference_tree = self.create_tree(
            summary,
            "Audios de referencia",
            remove_selected_command=self.remove_selected_references,
            clear_command=self.clear_references,
            clear_text="Remover audios",
            selected_text="Remover audios selecionados",
        )
        self.target_tree = self.create_tree(
            summary,
            "Videos das cameras",
            remove_selected_command=self.remove_selected_targets,
            clear_command=self.clear_targets,
            clear_text="Remover videos",
            selected_text="Remover videos selecionados",
        )
        self.reference_tree.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.target_tree.grid(row=0, column=1, sticky="nsew", padx=(8, 0))

        execution = ttk.LabelFrame(self, text="Execucao", padding=10)
        execution.grid(row=3, column=0, sticky="nsew", padx=16, pady=(0, 12))
        execution.columnconfigure(0, weight=1)
        execution.rowconfigure(1, weight=1)

        ttk.Label(execution, textvariable=self.status).grid(row=0, column=0, sticky="w")
        self.log = ScrolledText(execution, height=12, wrap="word")
        self.log.grid(row=1, column=0, sticky="nsew", pady=(8, 0))

    def create_tree(
        self,
        master: tk.Misc,
        heading: str,
        *,
        remove_selected_command,
        clear_command,
        clear_text: str,
        selected_text: str,
    ) -> ttk.Frame:
        frame = ttk.Frame(master)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        ttk.Label(frame, text=heading, font=("Segoe UI", 10, "bold")).grid(
            row=0,
            column=0,
            sticky="w",
            pady=(0, 4),
        )
        tree = ttk.Treeview(
            frame,
            columns=("count", "path"),
            displaycolumns=("count",),
            show="tree headings",
            height=8,
            selectmode="extended",
        )
        tree.heading("#0", text="Grupo / arquivo")
        tree.heading("count", text="Qtd")
        tree.column("#0", width=360)
        tree.column("count", width=56, anchor="center")
        tree.column("path", width=0, stretch=False)
        tree.grid(row=1, column=0, sticky="nsew")

        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        tree.configure(yscrollcommand=scrollbar.set)

        buttons = ttk.Frame(frame)
        buttons.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        buttons.columnconfigure(0, weight=1)
        buttons.columnconfigure(1, weight=1)

        clear_button = ttk.Button(buttons, text=clear_text, command=clear_command)
        selected_button = ttk.Button(
            buttons,
            text=selected_text,
            command=remove_selected_command,
        )
        clear_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        selected_button.grid(row=0, column=1, sticky="ew", padx=(4, 0))

        if clear_text.endswith("audios"):
            self.clear_audio_button = clear_button
            self.remove_audio_selected_button = selected_button
        else:
            self.clear_video_button = clear_button
            self.remove_video_selected_button = selected_button
        return frame

    def choose_references(self) -> None:
        selected = filedialog.askopenfilenames(
            title="Escolher audios de referencia",
            filetypes=supported_filetypes(AUDIO_EXTENSIONS),
        )
        if not selected:
            return
        added, warnings = add_files_grouped_by_parent(
            self.references,
            list(selected),
            extensions=AUDIO_EXTENSIONS,
        )
        self.after_files_added("audios", added, warnings)

    def choose_targets(self) -> None:
        selected = filedialog.askopenfilenames(
            title="Escolher videos das cameras",
            filetypes=supported_filetypes(VIDEO_EXTENSIONS),
        )
        if not selected:
            return
        added, warnings = add_files_grouped_by_parent(
            self.targets,
            list(selected),
            extensions=VIDEO_EXTENSIONS,
        )
        self.after_files_added("videos", added, warnings)

    def after_files_added(self, label: str, added: int, warnings: list[str]) -> None:
        self.refresh_summary()
        if warnings:
            messagebox.showwarning(
                "Alguns arquivos foram ignorados",
                "\n".join(warnings[:12]) + ("\n..." if len(warnings) > 12 else ""),
            )
        self.status.set(f"{added} {label} adicionados. Clique em Sincronizar quando estiver pronto.")

    def refresh_summary(self) -> None:
        self.populate_tree(self.reference_tree, self.references)
        self.populate_tree(self.target_tree, self.targets)

        refs = count_files(self.references)
        targets = count_files(self.targets)
        if refs or targets:
            self.status.set(f"{refs} audio(s) e {targets} video(s) escolhidos.")
        else:
            self.status.set("Escolha os audios e videos para comecar.")

    def populate_tree(self, frame: ttk.Frame, groups: dict[str, list[str]]) -> None:
        tree = self.tree_from_frame(frame)
        tree.delete(*tree.get_children())
        for group_name, paths in groups.items():
            group_id = tree.insert("", END, text=group_name, values=(len(paths), ""), open=True)
            for path in paths:
                tree.insert(group_id, END, text=Path(path).name, values=("", path))

    def tree_from_frame(self, frame: ttk.Frame) -> ttk.Treeview:
        return next(child for child in frame.winfo_children() if isinstance(child, ttk.Treeview))

    def remove_selected_references(self) -> None:
        self.remove_selected_media(
            self.reference_tree,
            self.references,
            empty_message="Selecione um audio ou grupo de audio para remover.",
        )

    def remove_selected_targets(self) -> None:
        self.remove_selected_media(
            self.target_tree,
            self.targets,
            empty_message="Selecione um video ou grupo de video para remover.",
        )

    def remove_selected_media(
        self,
        frame: ttk.Frame,
        groups: dict[str, list[str]],
        *,
        empty_message: str,
    ) -> None:
        tree = self.tree_from_frame(frame)
        selected_items = tree.selection()
        if not selected_items:
            messagebox.showinfo("Remover selecionados", empty_message)
            return

        groups_to_remove: set[str] = set()
        paths_to_remove: dict[str, set[str]] = {}
        for item_id in selected_items:
            parent_id = tree.parent(item_id)
            if not parent_id:
                groups_to_remove.add(str(tree.item(item_id, "text")))
                continue

            group_name = str(tree.item(parent_id, "text"))
            path_value = str(tree.set(item_id, "path") or "")
            if path_value:
                paths_to_remove.setdefault(group_name, set()).add(path_value)

        removed_count = 0
        for group_name in groups_to_remove:
            removed_count += len(groups.get(group_name, []))
            groups.pop(group_name, None)

        for group_name, selected_paths in paths_to_remove.items():
            if group_name in groups_to_remove or group_name not in groups:
                continue
            before = len(groups[group_name])
            groups[group_name] = [
                path for path in groups[group_name] if path not in selected_paths
            ]
            removed_count += before - len(groups[group_name])
            if not groups[group_name]:
                groups.pop(group_name, None)

        self.refresh_summary()
        self.status.set(f"{removed_count} arquivo(s) removido(s).")

    def clear_references(self) -> None:
        self.clear_media_group(
            self.references,
            label="audios",
        )

    def clear_targets(self) -> None:
        self.clear_media_group(
            self.targets,
            label="videos",
        )

    def clear_media_group(self, groups: dict[str, list[str]], *, label: str) -> None:
        total = count_files(groups)
        if total == 0:
            messagebox.showinfo("Remover arquivos", f"Nenhum {label} escolhido.")
            return
        if not messagebox.askyesno(
            "Remover arquivos",
            f"Remover todos os {total} {label} escolhidos?",
        ):
            return
        groups.clear()
        self.refresh_summary()
        self.status.set(f"Todos os {label} foram removidos.")

    def project_slug(self) -> str:
        return slugify(self.project_name.get())

    def selection_path(self) -> Path:
        return SELECTIONS_DIR / f"{self.project_slug()}.json"

    def output_xml(self) -> str:
        return self.last_xml_output or f"output/{self.project_slug()}.xml"

    def selection_payload(self, *, xml_output: str | None = None) -> dict:
        return {
            "name": self.project_name.get().strip() or self.project_slug(),
            "reference_groups": self.references,
            "target_groups": self.targets,
            "ignore_metadata": True,
            "use_camera_clock_model": True,
            "explicit_selection": True,
            "output": xml_output or self.output_xml(),
        }

    def validate_before_sync(self) -> bool:
        if not count_files(self.references):
            messagebox.showerror("Falta audio", "Escolha pelo menos um audio de referencia.")
            return False
        if not count_files(self.targets):
            messagebox.showerror("Falta video", "Escolha pelo menos um video de camera.")
            return False
        return True

    def choose_xml_output(self) -> str | None:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        initial_path = Path(self.output_xml())
        if not initial_path.is_absolute():
            initial_dir = OUTPUT_DIR
            initial_file = initial_path.name
        else:
            initial_dir = initial_path.parent
            initial_file = initial_path.name

        selected = filedialog.asksaveasfilename(
            title="Salvar XML sincronizado",
            initialdir=str(initial_dir),
            initialfile=initial_file or f"{self.project_slug()}.xml",
            defaultextension=".xml",
            filetypes=[
                ("XML para Premiere", "*.xml"),
                ("Todos os arquivos", "*.*"),
            ],
        )
        if not selected:
            return None

        output_path = Path(selected).expanduser()
        if output_path.suffix.casefold() != ".xml":
            output_path = output_path.with_suffix(".xml")
        self.last_xml_output = str(output_path)
        return self.last_xml_output

    def save_selection(self, *, xml_output: str) -> Path:
        path = self.selection_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                self.selection_payload(xml_output=xml_output),
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return path

    def send_diagnostic(self) -> None:
        if self.running_process is not None or self.diagnostic_running:
            messagebox.showinfo("Processo em andamento", "Aguarde o processo atual terminar.")
            return

        support_config: SupportConfig | None = None
        config_error: Exception | None = None
        try:
            support_config = load_support_config(PROJECT_ROOT)
        except Exception as exc:
            config_error = exc

        if support_config is None:
            consent = messagebox.askyesno(
                "Suporte nao configurado",
                f"{config_error}\n\n"
                "O WaveSync ainda pode gerar um pacote .zip local do ultimo sync, "
                "sem enviar pela internet.\n\n"
                "Depois voce pode mandar esse .zip manualmente para o suporte.\n\n"
                "Deseja gerar o ZIP agora?",
            )
            if not consent:
                self.status.set("Diagnostico cancelado.")
                return

        self.diagnostic_running = True
        self.set_buttons_enabled(False)
        self.append_log("\n" + "=" * 72 + "\nDIAGNOSTICO\n" + "=" * 72 + "\n")
        self.append_log("Gerando pacote de diagnostico...\n")
        if support_config is None:
            self.status.set("Gerando diagnostico local...")
        else:
            self.status.set("Enviando diagnostico para suporte...")

        thread = threading.Thread(
            target=self._send_diagnostic_worker,
            args=(support_config,),
            daemon=True,
        )
        thread.start()

    def _send_diagnostic_worker(self, support_config: SupportConfig | None) -> None:
        try:
            package = create_diagnostic_package(PROJECT_ROOT)
            self.log_queue.put(("line", f"Pacote: {package.path}\n"))
            self.log_queue.put(("line", f"Arquivos incluidos: {len(package.included_files)}\n"))
            if package.skipped_files:
                self.log_queue.put(("line", f"Arquivos ignorados: {len(package.skipped_files)}\n"))

            if support_config is None:
                self.log_queue.put((
                    "line",
                    "[OK] Diagnostico salvo localmente. Envie o ZIP manualmente para o suporte.\n",
                ))
                self.log_queue.put(("diag_done", {"return_code": 0, "sent": False, "path": str(package.path)}))
                return

            result = send_diagnostic_package(package.path, support_config)
            suffix = f" message_id={result.message_id}" if result.message_id else ""
            self.log_queue.put(("line", f"[OK] Diagnostico enviado para suporte.{suffix}\n"))
            self.log_queue.put(("diag_done", {"return_code": 0, "sent": True, "path": str(package.path)}))
        except Exception as exc:
            self.log_queue.put(("line", f"[ERROR] Falha ao gerar/enviar diagnostico: {exc}\n"))
            self.log_queue.put(("diag_done", {"return_code": 1, "sent": False, "path": ""}))

    def run_sync(self) -> None:
        if self.running_process is not None:
            messagebox.showinfo("Sync em andamento", "Aguarde o processo atual terminar.")
            return
        if not self.validate_before_sync():
            return

        xml_output = self.choose_xml_output()
        if not xml_output:
            self.status.set("Sync cancelado. Nenhum XML foi escolhido.")
            return

        selection = self.save_selection(xml_output=xml_output)
        command = build_sync_command(selection, xml_output)

        self.current_sync_output = []
        self.collecting_sync_output = True
        self.set_buttons_enabled(False)
        self.append_log("\n" + "=" * 72 + "\nSYNC\n" + "=" * 72 + "\n")
        self.append_log(f"Selection: {selection}\n")
        self.append_log(f"XML      : {xml_output}\n")
        self.append_log(format_command(command) + "\n\n")
        self.status.set("Sincronizando... isso pode demorar alguns minutos.")

        thread = threading.Thread(target=self._run_worker, args=(command,), daemon=True)
        thread.start()

    def _run_worker(self, command: list[str]) -> None:
        try:
            process = subprocess.Popen(
                command,
                cwd=PROJECT_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env={
                    **os.environ,
                    "PYTHONIOENCODING": "utf-8",
                    "PYTHONUTF8": "1",
                },
                **hidden_subprocess_options(),
            )
            self.running_process = process
            if process.stdout is not None:
                for line in process.stdout:
                    self.log_queue.put(("line", line))
            return_code = process.wait()
            self.log_queue.put(("done", return_code))
        except Exception as exc:
            self.log_queue.put(("line", f"[ERROR] {exc}\n"))
            self.log_queue.put(("done", 1))

    def drain_log_queue(self) -> None:
        try:
            while True:
                kind, value = self.log_queue.get_nowait()
                if kind == "line":
                    line = str(value)
                    if self.collecting_sync_output:
                        self.current_sync_output.append(line)
                    self.append_log(line)
                elif kind == "done":
                    return_code = int(value)
                    self.running_process = None
                    self.collecting_sync_output = False
                    self.set_buttons_enabled(True)
                    outcome = self.sync_outcome_from_log(return_code)
                    self.status.set(outcome["status_text"])
                    self.append_log(outcome["log_line"])
                    self.show_sync_result_dialog(
                        title=outcome["title"],
                        message=outcome["message"],
                        text_color=outcome["color"],
                    )
                elif kind == "diag_done":
                    if isinstance(value, dict):
                        return_code = int(value.get("return_code", 1))
                        sent = bool(value.get("sent"))
                        package_path = str(value.get("path") or "")
                    else:
                        return_code = int(value)
                        sent = return_code == 0
                        package_path = ""
                    self.diagnostic_running = False
                    self.set_buttons_enabled(True)
                    if return_code == 0 and sent:
                        self.status.set("Diagnostico enviado para suporte.")
                        messagebox.showinfo("Diagnostico enviado", "Pacote de diagnostico enviado para suporte.")
                    elif return_code == 0:
                        self.status.set("Diagnostico salvo localmente.")
                        messagebox.showinfo(
                            "Diagnostico salvo",
                            "Pacote de diagnostico salvo localmente:\n"
                            f"{package_path}\n\n"
                            "Envie esse ZIP manualmente para o suporte.",
                        )
                    else:
                        self.status.set("Falha ao gerar/enviar diagnostico. Veja o log abaixo.")
        except queue.Empty:
            pass
        self.after(100, self.drain_log_queue)

    def sync_outcome_from_log(self, return_code: int) -> dict[str, str]:
        log_text = "".join(self.current_sync_output)
        sync_check = latest_summary_value(log_text, "SyncCheck")
        sync_guard = latest_summary_value(log_text, "SyncGuard")
        xml_output = self.output_xml()

        if return_code != 0 or sync_guard.casefold().startswith("bloqueio"):
            return {
                "title": "Sync precisa de revisao",
                "message": (
                    "O WaveSync nao aprovou esse resultado automaticamente.\n\n"
                    f"SyncCheck: {sync_check or 'n/a'}\n"
                    f"SyncGuard: {sync_guard or 'n/a'}\n\n"
                    "Veja o resumo no log e confira os alertas antes de usar o XML."
                ),
                "color": "#c62828",
                "status_text": "Sync precisa de revisao. Veja o log abaixo.",
                "log_line": f"\n[FAIL] Sync finalizado com exit={return_code}.\n",
            }

        if sync_check.casefold().startswith("atencao"):
            return {
                "title": "Sync concluido com atencao",
                "message": (
                    "O XML foi gerado, mas o WaveSync encontrou algumas inconsistencias.\n\n"
                    f"SyncCheck: {sync_check}\n"
                    f"XML: {xml_output}\n\n"
                    "Importe no Premiere e confira os clipes indicados no log."
                ),
                "color": "#b58900",
                "status_text": "Sync concluido com atencao. Confira o log.",
                "log_line": "\n[WARN] Sync concluido com atencao.\n",
            }

        return {
            "title": "Sync concluido",
            "message": f"XML gerado:\n{xml_output}",
            "color": "#111111",
            "status_text": "Sync concluido. Importe o XML no Premiere.",
            "log_line": "\n[OK] Sync concluido.\n",
        }

    def show_sync_result_dialog(self, *, title: str, message: str, text_color: str) -> None:
        dialog = tk.Toplevel(self)
        dialog.title(title)
        dialog.transient(self)
        dialog.resizable(False, False)

        container = ttk.Frame(dialog, padding=20)
        container.grid(row=0, column=0, sticky="nsew")
        container.columnconfigure(0, weight=1)

        tk.Label(
            container,
            text=title,
            foreground=text_color,
            font=("Segoe UI", 12, "bold"),
            anchor="w",
            justify="left",
        ).grid(row=0, column=0, sticky="ew")
        tk.Label(
            container,
            text=message,
            foreground=text_color,
            font=("Segoe UI", 10),
            anchor="w",
            justify="left",
            wraplength=520,
        ).grid(row=1, column=0, sticky="ew", pady=(12, 18))

        ok_button = ttk.Button(container, text="OK", command=dialog.destroy)
        ok_button.grid(row=2, column=0, sticky="e")
        ok_button.focus_set()
        dialog.bind("<Return>", lambda _event: dialog.destroy())
        dialog.bind("<Escape>", lambda _event: dialog.destroy())

        dialog.update_idletasks()
        x = self.winfo_rootx() + max(0, (self.winfo_width() - dialog.winfo_width()) // 2)
        y = self.winfo_rooty() + max(0, (self.winfo_height() - dialog.winfo_height()) // 2)
        dialog.geometry(f"+{x}+{y}")
        dialog.grab_set()

    def set_buttons_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        self.audio_button.configure(state=state)
        self.video_button.configure(state=state)
        self.sync_button.configure(state=state)
        self.diagnostic_button.configure(state=state)
        self.clear_audio_button.configure(state=state)
        self.remove_audio_selected_button.configure(state=state)
        self.clear_video_button.configure(state=state)
        self.remove_video_selected_button.configure(state=state)

    def append_log(self, text: str) -> None:
        self.log.insert(END, text)
        self.log.see(END)


def main() -> int:
    args = parse_cli_args()
    if args.sync_worker:
        if not args.selection or not args.xml_output:
            print("[ERROR] --sync-worker exige --selection e --xml-output", file=sys.stderr)
            return 2
        return run_embedded_sync_worker(args.selection, args.xml_output)

    app = WaveSyncSimpleApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
