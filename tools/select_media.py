"""Create an explicit media selection JSON for the sync pipeline.

Interactive usage:
  python tools/select_media.py

Convert an existing config into a selection:
  python tools/select_media.py --from-config configs/casamento.json --dry-run
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from make_config import AUDIO_EXTENSIONS, VIDEO_EXTENSIONS, flatten_string_list, slugify


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SELECTION_DIR = PROJECT_ROOT / "selections"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Cria uma selection JSON com arquivos escolhidos explicitamente."
    )
    parser.add_argument(
        "--name",
        default=None,
        help="Nome da selection/projeto. Padrao: pergunta no modo interativo.",
    )
    parser.add_argument(
        "--selection-output",
        dest="selection_output",
        default=None,
        help="Caminho do JSON de selection. Padrao: selections/<name>.json.",
    )
    parser.add_argument(
        "--xml-output",
        default=None,
        help="Caminho do XML que sera gravado na selection. Padrao: output/<name>.xml.",
    )
    parser.add_argument(
        "--from-config",
        default=None,
        help="Converte um config existente em selection explicita.",
    )
    parser.add_argument(
        "--use-metadata",
        action="store_true",
        help="Grava ignore_metadata=false. Padrao: true.",
    )
    parser.add_argument(
        "--no-clock-model",
        action="store_true",
        help="Grava use_camera_clock_model=false. Padrao: true.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Sobrescreve selection existente.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Mostra a selection, mas nao grava arquivo.",
    )
    return parser.parse_args(argv)


def prompt_text(prompt: str, *, default: str | None = None, required: bool = True) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            value = input(f"{prompt}{suffix}: ").strip()
        except EOFError as exc:
            if default is not None:
                return default
            raise RuntimeError(f"Entrada obrigatoria nao informada: {prompt}") from exc

        if value:
            return value
        if default is not None:
            return default
        if not required:
            return ""
        print("Informe um valor.")


def prompt_yes_no(prompt: str, *, default: bool = False) -> bool:
    suffix = " [S/n]" if default else " [s/N]"
    while True:
        value = input(f"{prompt}{suffix}: ").strip().casefold()
        if not value:
            return default
        if value in {"s", "sim", "y", "yes"}:
            return True
        if value in {"n", "nao", "não", "no"}:
            return False
        print("Responda s ou n.")


def split_path_line(line: str) -> list[str]:
    text = line.strip()
    if not text:
        return []
    if '"' not in text and "'" not in text:
        return [text]

    entries: list[str] = []
    pattern = re.compile(r'"([^"]+)"|\'([^\']+)\'|(\S+)')
    for match in pattern.finditer(text):
        entry = match.group(1) or match.group(2) or match.group(3)
        if entry:
            entries.append(entry)
    return entries


def normalize_path_text(raw_path: str) -> Path:
    text = raw_path.strip().strip('"').strip("'")
    return Path(text).expanduser()


def validate_selected_file(raw_path: str, extensions: set[str], label: str) -> Path:
    path = normalize_path_text(raw_path)
    if not path.exists():
        raise FileNotFoundError(f"{label} nao encontrado: {path}")
    if not path.is_file():
        raise ValueError(f"{label} deve ser arquivo, nao pasta: {path}")
    if path.suffix.casefold() not in extensions:
        supported = ", ".join(sorted(extensions))
        raise ValueError(f"{label} com extensao invalida: {path} | suportadas: {supported}")

    path_text = " ".join(path.parts).casefold()
    if extensions == VIDEO_EXTENSIONS and "proxy" in path_text:
        raise ValueError(f"Proxy selecionado por engano: {path}")
    if extensions == AUDIO_EXTENSIONS and (
        "drift_corrected" in path_text or "pluraleyes_drift_corrected" in path_text
    ):
        raise ValueError(f"Referencia drift_corrected selecionada por engano: {path}")
    return path.resolve()


def path_for_selection(path: Path) -> str:
    return path.resolve().as_posix()


def collect_group_files(
    *,
    label: str,
    extensions: set[str],
    seen_paths: dict[str, str],
) -> list[str]:
    print("")
    print(f"Cole/arraste os arquivos de {label}.")
    print("Pode colar um arquivo por linha, ou varios caminhos entre aspas na mesma linha.")
    print("Linha vazia finaliza este grupo.")

    selected: list[str] = []
    while True:
        line = input("> ").strip()
        if not line:
            if selected:
                return selected
            print("Nenhum arquivo neste grupo ainda.")
            if prompt_yes_no("Cancelar este grupo?", default=True):
                return []
            continue

        for raw_path in split_path_line(line):
            try:
                path = validate_selected_file(raw_path, extensions, label)
                path_key = str(path).casefold()
                if path_key in seen_paths:
                    print(f"[WARN] Duplicado ignorado: {path} ja esta em {seen_paths[path_key]}")
                    continue
                seen_paths[path_key] = label
                selected.append(path_for_selection(path))
                print(f"[OK] {path.name}")
            except Exception as exc:
                print(f"[WARN] {exc}")


def collect_groups(kind_label: str, extensions: set[str]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    seen_paths: dict[str, str] = {}

    print("")
    print("=" * 72)
    print(kind_label.upper())
    print("=" * 72)

    while True:
        group_name = prompt_text(
            f"Nome do grupo de {kind_label} (Enter para terminar)",
            required=False,
        )
        if not group_name:
            if groups:
                return groups
            print(f"Crie pelo menos um grupo de {kind_label}.")
            continue

        if group_name in groups:
            print("Esse grupo ja existe. Use outro nome.")
            continue

        files = collect_group_files(
            label=group_name,
            extensions=extensions,
            seen_paths=seen_paths,
        )
        if not files:
            print("[WARN] Grupo vazio ignorado.")
            continue
        groups[group_name] = files
        print(f"[OK] Grupo {group_name}: {len(files)} arquivo(s)")


def read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"JSON deve conter um objeto: {path}")
    return payload


def grouped_from_config(value: object, label: str) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} do config deve ser um objeto com grupos.")

    grouped: dict[str, list[str]] = {}
    for raw_group_name, raw_paths in value.items():
        group_name = str(raw_group_name)
        paths = flatten_string_list(raw_paths, f"{label}.{group_name}")
        if not paths:
            raise ValueError(f"{label}.{group_name} esta vazio.")
        grouped[group_name] = paths
    return grouped


def build_payload_from_config(args: argparse.Namespace) -> dict:
    config_path = Path(args.from_config).expanduser()
    if not config_path.exists():
        raise FileNotFoundError(f"Config nao encontrado: {config_path}")

    config = read_json(config_path)
    project_name = args.name or config_path.stem
    xml_output = args.xml_output or str(config.get("output") or f"output/{slugify(project_name)}.xml")

    return {
        "name": project_name,
        "reference_groups": grouped_from_config(config.get("references"), "references"),
        "target_groups": grouped_from_config(config.get("targets"), "targets"),
        "ignore_metadata": bool(config.get("ignore_metadata", not args.use_metadata)),
        "use_camera_clock_model": bool(
            config.get("use_camera_clock_model", not args.no_clock_model)
        ),
        "output": xml_output,
    }


def build_payload_interactive(args: argparse.Namespace) -> dict:
    print("")
    print("=" * 72)
    print("CRIAR SELECTION EXPLICITA")
    print("=" * 72)
    print("Este arquivo define exatamente quais audios e videos entram no sync.")

    project_name = args.name or prompt_text("Nome do casamento/projeto")
    preset_name = slugify(project_name)
    xml_output = args.xml_output or prompt_text(
        "Output XML",
        default=f"output/{preset_name}.xml",
    )

    references = collect_groups("referencias de audio", AUDIO_EXTENSIONS)
    targets = collect_groups("videos de camera", VIDEO_EXTENSIONS)

    return {
        "name": project_name,
        "reference_groups": references,
        "target_groups": targets,
        "ignore_metadata": not args.use_metadata,
        "use_camera_clock_model": not args.no_clock_model,
        "output": xml_output,
    }


def selection_output_path(args: argparse.Namespace, payload: dict) -> Path:
    if args.selection_output:
        return Path(args.selection_output).expanduser()
    name = str(payload.get("name") or "selection")
    return DEFAULT_SELECTION_DIR / f"{slugify(name)}.json"


def count_grouped(groups: dict[str, list[str]]) -> int:
    return sum(len(paths) for paths in groups.values())


def print_summary(payload: dict, output_path: Path) -> None:
    references = payload.get("reference_groups") or {}
    targets = payload.get("target_groups") or {}
    print("")
    print("=" * 72)
    print("SELECTION GERADA")
    print("=" * 72)
    print(f"Arquivo selection : {output_path}")
    print(f"Nome              : {payload.get('name')}")
    print(f"Output XML        : {payload.get('output')}")
    print(f"Referencias       : {count_grouped(references)} arquivo(s) em {len(references)} grupo(s)")
    for group_name, paths in references.items():
        print(f"  - {group_name}: {len(paths)}")
    print(f"Targets           : {count_grouped(targets)} arquivo(s) em {len(targets)} grupo(s)")
    for group_name, paths in targets.items():
        print(f"  - {group_name}: {len(paths)}")
    print(f"Ignore metadata   : {payload.get('ignore_metadata')}")
    print(f"Clock model       : {payload.get('use_camera_clock_model')}")
    print("=" * 72)


def write_selection(path: Path, payload: dict, *, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"Selection ja existe: {path}. Use --overwrite.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def print_next_commands(selection_path: Path) -> None:
    print("")
    print("Proximos comandos:")
    print(f'  python tools\\make_config.py --from-selection "{selection_path}"')
    print(f'  python tools\\sync_from_selection.py --selection "{selection_path}"')


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        payload = (
            build_payload_from_config(args)
            if args.from_config
            else build_payload_interactive(args)
        )
        output_path = selection_output_path(args, payload)
        print_summary(payload, output_path)

        if args.dry_run:
            print("")
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if output_path.exists() and not args.overwrite and sys.stdin.isatty():
            if prompt_yes_no(f"Selection ja existe. Sobrescrever {output_path}?", default=False):
                args.overwrite = True

        write_selection(output_path, payload, overwrite=args.overwrite)
        print("")
        print(f"[OK] Selection salva: {output_path}")
        print_next_commands(output_path)
        return 0
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
