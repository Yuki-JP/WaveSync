"""Streamlit UI for the PluralEyes clone workflow.

Run:
  python -m streamlit run streamlit_app.py
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import streamlit as st


PROJECT_ROOT = Path(__file__).resolve().parent
SELECTIONS_DIR = PROJECT_ROOT / "selections"
CONFIGS_DIR = PROJECT_ROOT / "configs"
OUTPUT_DIR = PROJECT_ROOT / "output"

AUDIO_EXTENSIONS = {".wav", ".wave", ".mp3", ".aac", ".m4a", ".flac", ".ogg", ".wma"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".mts"}


def slugify(text: str) -> str:
    normalized = text.strip().casefold()
    normalized = re.sub(r"[^a-z0-9]+", "_", normalized)
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return normalized or "projeto"


def init_state() -> None:
    defaults = {
        "project_name": "novo_casamento",
        "output_xml": "output/novo_casamento.xml",
        "selection_path": "selections/novo_casamento.json",
        "reference_groups": {},
        "target_groups": {},
        "ignore_metadata": True,
        "use_camera_clock_model": True,
        "last_log": "",
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def selection_abs_path() -> Path:
    path = Path(st.session_state.selection_path).expanduser()
    if path.is_absolute():
        return path
    return (PROJECT_ROOT / path).resolve()


def parse_paths(text: str) -> list[str]:
    paths: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if '"' in line or "'" in line:
            pattern = re.compile(r'"([^"]+)"|\'([^\']+)\'')
            matches = [match.group(1) or match.group(2) for match in pattern.finditer(line)]
            if matches:
                paths.extend(matches)
                continue
        paths.append(line.strip().strip('"').strip("'"))
    return paths


def add_paths(group_key: str, group_name: str, paths: list[str]) -> int:
    groups = st.session_state[group_key]
    current = list(groups.get(group_name, []))
    existing = {str(Path(item)).casefold() for item in current}
    added = 0
    for raw_path in paths:
        path = str(Path(raw_path.strip().strip('"').strip("'")).expanduser())
        if not path:
            continue
        key = str(Path(path)).casefold()
        if key in existing:
            continue
        current.append(path)
        existing.add(key)
        added += 1
    groups[group_name] = current
    return added


def pick_files(title: str, extensions: set[str]) -> list[str]:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:
        st.error(f"Seletor nativo indisponivel: {exc}")
        return []

    patterns = " ".join(f"*{extension}" for extension in sorted(extensions))
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        files = filedialog.askopenfilenames(
            title=title,
            filetypes=[
                ("Arquivos suportados", patterns),
                ("Todos os arquivos", "*.*"),
            ],
        )
    finally:
        root.destroy()
    return [str(Path(path)) for path in files]


def selection_payload() -> dict:
    return {
        "name": st.session_state.project_name,
        "reference_groups": st.session_state.reference_groups,
        "target_groups": st.session_state.target_groups,
        "ignore_metadata": bool(st.session_state.ignore_metadata),
        "use_camera_clock_model": bool(st.session_state.use_camera_clock_model),
        "explicit_selection": True,
        "output": st.session_state.output_xml,
    }


def write_selection() -> Path:
    path = selection_abs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(selection_payload(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def load_selection(path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Selection precisa conter um objeto JSON.")

    references = payload.get("reference_groups") or payload.get("references") or {}
    targets = payload.get("target_groups") or payload.get("targets") or {}
    if not isinstance(references, dict) or not isinstance(targets, dict):
        raise ValueError("Selection precisa ter grupos de referencias e targets.")

    st.session_state.project_name = str(payload.get("name") or path.stem)
    st.session_state.output_xml = str(payload.get("output") or f"output/{path.stem}.xml")
    st.session_state.selection_path = str(path)
    st.session_state.reference_groups = {
        str(group): [str(item) for item in items]
        for group, items in references.items()
        if isinstance(items, list)
    }
    st.session_state.target_groups = {
        str(group): [str(item) for item in items]
        for group, items in targets.items()
        if isinstance(items, list)
    }
    st.session_state.ignore_metadata = bool(payload.get("ignore_metadata", True))
    st.session_state.use_camera_clock_model = bool(
        payload.get("use_camera_clock_model", True)
    )


def format_command(command: list[str]) -> str:
    return " ".join(f'"{item}"' if " " in item else item for item in command)


def run_command(command: list[str]) -> int:
    output = st.empty()
    lines: list[str] = []
    st.session_state.last_log = ""

    process = subprocess.Popen(
        command,
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    if process.stdout is not None:
        for line in process.stdout:
            lines.append(line.rstrip())
            st.session_state.last_log = "\n".join(lines)
            output.code("\n".join(lines[-350:]), language="text")

    return_code = process.wait()
    if not lines:
        output.code("(sem saida)", language="text")
    return int(return_code)


def validate_selection(selection_path: Path) -> int:
    command = [
        sys.executable,
        str(PROJECT_ROOT / "tools" / "validate_selection.py"),
        "--selection",
        str(selection_path),
        "--xml-output",
        st.session_state.output_xml,
    ]
    st.caption(format_command(command))
    return run_command(command)


def generate_config(selection_path: Path) -> int:
    command = [
        sys.executable,
        str(PROJECT_ROOT / "tools" / "make_config.py"),
        "--from-selection",
        str(selection_path),
        "--xml-output",
        st.session_state.output_xml,
        "--overwrite",
    ]
    st.caption(format_command(command))
    return run_command(command)


def run_sync(selection_path: Path) -> int:
    command = [
        sys.executable,
        str(PROJECT_ROOT / "tools" / "sync_from_selection.py"),
        "--selection",
        str(selection_path),
        "--xml-output",
        st.session_state.output_xml,
        "--python",
        sys.executable,
    ]
    st.caption(format_command(command))
    return run_command(command)


def render_group_editor(
    *,
    title: str,
    state_key: str,
    extensions: set[str],
    add_label: str,
) -> None:
    st.subheader(title)

    with st.container(border=True):
        new_group = st.text_input(add_label, key=f"new_{state_key}")
        if st.button("Adicionar grupo", key=f"add_{state_key}", use_container_width=True):
            name = new_group.strip()
            if not name:
                st.warning("Informe um nome de grupo.")
            elif name in st.session_state[state_key]:
                st.warning("Esse grupo ja existe.")
            else:
                st.session_state[state_key][name] = []
                st.success(f"Grupo criado: {name}")

    for group_name in list(st.session_state[state_key].keys()):
        paths = st.session_state[state_key][group_name]
        with st.expander(f"{group_name} ({len(paths)} arquivo(s))", expanded=True):
            col_pick, col_clear, col_remove = st.columns(3)
            if col_pick.button(
                "Selecionar arquivos",
                key=f"pick_{state_key}_{group_name}",
                use_container_width=True,
            ):
                selected = pick_files(f"Selecionar arquivos - {group_name}", extensions)
                added = add_paths(state_key, group_name, selected)
                st.success(f"{added} arquivo(s) adicionado(s).")

            if col_clear.button(
                "Limpar grupo",
                key=f"clear_{state_key}_{group_name}",
                use_container_width=True,
            ):
                st.session_state[state_key][group_name] = []
                st.info("Grupo limpo.")

            if col_remove.button(
                "Remover grupo",
                key=f"remove_{state_key}_{group_name}",
                use_container_width=True,
            ):
                del st.session_state[state_key][group_name]
                st.info("Grupo removido.")
                st.rerun()

            pasted = st.text_area(
                "Cole caminhos aqui",
                key=f"paste_{state_key}_{group_name}",
                placeholder='"D:\\evento\\arquivo1.MP4"\n"D:\\evento\\arquivo2.MP4"',
                height=110,
            )
            if st.button(
                "Adicionar caminhos colados",
                key=f"add_pasted_{state_key}_{group_name}",
                use_container_width=True,
            ):
                parsed = parse_paths(pasted)
                added = add_paths(state_key, group_name, parsed)
                st.success(f"{added} arquivo(s) adicionado(s).")

            if paths:
                st.dataframe(
                    [{"arquivo": Path(path).name, "caminho": path} for path in paths],
                    use_container_width=True,
                    hide_index=True,
                )


def render_sidebar() -> None:
    with st.sidebar:
        st.header("Projeto")
        st.text_input("Nome", key="project_name")

        slug = slugify(st.session_state.project_name)
        if st.button("Usar nomes padrao pelo projeto", use_container_width=True):
            st.session_state.output_xml = f"output/{slug}.xml"
            st.session_state.selection_path = f"selections/{slug}.json"
            st.rerun()

        st.text_input("Selection JSON", key="selection_path")
        st.text_input("Output XML", key="output_xml")
        st.checkbox("Ignorar metadata/mtime", key="ignore_metadata")
        st.checkbox("Usar clock model", key="use_camera_clock_model")

        st.divider()
        st.header("Abrir selection")
        load_path_text = st.text_input(
            "Caminho da selection existente",
            value=st.session_state.selection_path,
        )
        if st.button("Carregar selection", use_container_width=True):
            try:
                load_selection(Path(load_path_text).expanduser())
                st.success("Selection carregada.")
                st.rerun()
            except Exception as exc:
                st.error(str(exc))


def main() -> None:
    st.set_page_config(
        page_title="PluralEyes Clone",
        page_icon="🎬",
        layout="wide",
    )
    init_state()
    render_sidebar()

    st.title("PluralEyes Clone")
    st.caption("Selecione arquivos, valide a selection e gere o XML para Premiere.")

    tab_refs, tab_targets, tab_run, tab_json = st.tabs(
        ["Audios", "Videos", "Validar e sincronizar", "Selection JSON"]
    )

    with tab_refs:
        render_group_editor(
            title="Referencias de audio",
            state_key="reference_groups",
            extensions=AUDIO_EXTENSIONS,
            add_label="Novo grupo de audio (ex: lapela1, h4n, mesa)",
        )

    with tab_targets:
        render_group_editor(
            title="Videos de camera",
            state_key="target_groups",
            extensions=VIDEO_EXTENSIONS,
            add_label="Novo grupo de camera (ex: a7iv_victor, zve10_kaiky)",
        )

    with tab_run:
        st.subheader("Resumo")
        col_a, col_b, col_c = st.columns(3)
        ref_count = sum(len(items) for items in st.session_state.reference_groups.values())
        target_count = sum(len(items) for items in st.session_state.target_groups.values())
        col_a.metric("Audios", ref_count)
        col_b.metric("Videos", target_count)
        col_c.metric("Grupos", len(st.session_state.reference_groups) + len(st.session_state.target_groups))

        st.write(f"Selection: `{selection_abs_path()}`")
        st.write(f"XML: `{st.session_state.output_xml}`")

        col_save, col_validate, col_config, col_sync = st.columns(4)
        if col_save.button("Salvar selection", use_container_width=True):
            path = write_selection()
            st.success(f"Selection salva: {path}")

        if col_validate.button("Validar", use_container_width=True):
            path = write_selection()
            code = validate_selection(path)
            if code == 0:
                st.success("Selection valida.")
            else:
                st.error("Selection invalida.")

        if col_config.button("Gerar config", use_container_width=True):
            path = write_selection()
            code = generate_config(path)
            if code == 0:
                st.success("Config gerado.")
            else:
                st.error("Falha ao gerar config.")

        if col_sync.button("Sincronizar", type="primary", use_container_width=True):
            path = write_selection()
            code = run_sync(path)
            if code == 0:
                st.success("Sync concluido.")
            else:
                st.error("Sync falhou.")

        if st.session_state.last_log:
            st.download_button(
                "Baixar ultimo log",
                data=st.session_state.last_log,
                file_name=f"{slugify(st.session_state.project_name)}_sync_log.txt",
                mime="text/plain",
            )

    with tab_json:
        st.subheader("Preview")
        st.json(selection_payload())


if __name__ == "__main__":
    main()
