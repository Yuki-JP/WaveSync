# PluralEyes Clone - Sync Multicamera

Ferramenta Python para sincronizacao automatica de audio/video em fluxos reais de casamento, com foco em multicamera, lapelas continuas, mesa de som, cache DSP e export FCP XML para Premiere.

## Estado Atual

Versao funcional validada com o casamento Soho:

- 4 cameras
- 5 referencias de audio
- 34 videos
- 0 falhas
- `TrackCheck : OK`
- cache DSP validado

Checkpoint funcional no Git:

```powershell
git log -1 --oneline
```

## Estrutura

```text
main.py                 CLI principal de sincronizacao
backend/audio_processor.py
backend/xml_generator.py
backend/audit_report.py
configs/                Presets finais para main.py --config
selections/             Selecoes pequenas para gerar configs
golden/                 Baselines de regressao
tools/make_config.py    Gera config a partir de pastas/filtros/ranges
tools/sync_from_selection.py
tools/run_regression.py
tools/validate_golden.py
temp/                   cache e WAVs temporarios, ignorado pelo Git
output/                 XMLs/auditorias gerados, ignorado pelo Git
```

## Fluxo Recomendado

### 1. Criar config a partir de selection

Use um arquivo em `selections/` para manter exatamente os audios e videos que devem
entrar no sync. Esse e o fluxo recomendado para poupar tempo e evitar que arquivos
fora do trecho desejado entrem na timeline.

```powershell
python tools\make_config.py --from-selection "selections\casamento_soho_selection.json"
```

Para testar sem gravar:

```powershell
python tools\make_config.py --from-selection "selections\casamento_soho_selection.json" --dry-run
```

### 2. Rodar sincronizacao pelo config

```powershell
python main.py --config "configs\casamento_soho_auto.json"
```

O resultado esperado aparece no sumario:

```text
Sucesso    : 34 arquivo(s)
Falhas     : 0 arquivo(s)
TrackCheck : OK
Cache DSP  : refs 5/5 | targets 34/34
```

### 3. Comando unico: selection -> config -> sync

Depois que a selection estiver correta, rode tudo com um comando:

```powershell
python tools\sync_from_selection.py --selection "selections\casamento_soho_selection.json"
```

Modos uteis:

```powershell
python tools\sync_from_selection.py --selection "selections\casamento_soho_selection.json" --dry-run
python tools\sync_from_selection.py --selection "selections\casamento_soho_selection.json" --config-only
```

## Regressao

Antes de mexer no motor de sync, rode todos os baselines validados:

```powershell
python tools\run_regression.py
```

Hoje esse comando valida:

- `soho`
- `juliana-caue`

Para rodar apenas um caso:

```powershell
python tools\run_regression.py --case soho
python tools\run_regression.py --case juliana-caue
```

Modo rapido, sem reprocessar:

```powershell
python tools\run_regression.py --validate-only
```

Se o cache DSP tiver sido limpo:

```powershell
python tools\run_regression.py --validate-only --allow-cold-cache
```

## Criando Uma Nova Selection

### Modo recomendado: arquivos selecionados por grupo

Liste exatamente os arquivos escolhidos pelo usuario em `reference_groups` e
`target_groups`. O nome de cada grupo vira a base da organizacao do config.

```json
{
  "name": "casamento_exemplo",
  "reference_groups": {
    "lapela_01": [
      "D:/evento/02 AUDIOS/LAPELA 01/DJI_01.WAV",
      "D:/evento/02 AUDIOS/LAPELA 01/DJI_02.WAV"
    ],
    "mesa_h4n": [
      "D:/evento/02 AUDIOS/H4N/MONO-017.wav"
    ]
  },
  "target_groups": {
    "cam_01_a7iv_victor": [
      "D:/evento/01 CAMERAS/CAM 01/A7IV_9715.MP4",
      "D:/evento/01 CAMERAS/CAM 01/A7IV_9716.MP4"
    ],
    "cam_02_zve10_kenia": [
      "D:/evento/01 CAMERAS/CAM 02/ZVE10_9450.MP4",
      "D:/evento/01 CAMERAS/CAM 02/ZVE10_9451.MP4"
    ]
  },
  "ignore_metadata": true,
  "use_camera_clock_model": true,
  "output": "output/casamento_exemplo.xml"
}
```

### Atalho: pastas com filtros/ranges

Quando quiser gerar uma selection rapidamente a partir de uma pasta, ainda da para
usar filtros e ranges:

```json
{
  "name": "casamento_exemplo",
  "references": [
    "D:/evento/02 AUDIOS"
  ],
  "targets": [
    "D:/evento/01 CAMERAS"
  ],
  "reference_filter": [
    "DJI_01",
    "DJI_02",
    "MONO-017"
  ],
  "target_range": [
    "A7IV_20260411_9715..A7IV_20260411_9729",
    "ZVE10_02_20260411_9450..ZVE10_02_20260411_9453",
    "C0020..C0029"
  ],
  "ignore_metadata": true,
  "use_camera_clock_model": true,
  "output": "output/casamento_exemplo.xml"
}
```

Campos principais:

- `name`: nome do preset/config gerado.
- `reference_groups`: audios selecionados explicitamente por lapela/mesa.
- `target_groups`: videos selecionados explicitamente por camera.
- `references`: arquivos ou pastas de audio, para modo por filtro/range.
- `targets`: arquivos ou pastas de video, para modo por filtro/range.
- `reference_filter`: substrings/globs de audios permitidos no modo por pasta.
- `target_filter`: substrings/globs de videos permitidos no modo por pasta.
- `reference_range`: ranges inclusivos de audios no modo por pasta.
- `target_range`: ranges inclusivos de videos no modo por pasta.
- `ignore_metadata`: normalmente `true` para casamentos com relogios desalinhados.
- `use_camera_clock_model`: normalmente `true`.
- `output`: XML final.

## Arquivos Que Nao Devem Ser Commitados

O Git ignora:

- `temp/`
- `output/`
- `__pycache__/`
- cache DSP
- configs locais `configs/*_auto.json`

Commite somente configs/selection/golden que voce realmente quer preservar.

## Comandos De Referencia

Gerar config manual por CLI:

```powershell
python tools\make_config.py `
  --name casamento_soho_auto `
  -r "D:\2026-04-11 - Casamento - Debora Seimaru e Lucas - Soho\02 AUDIOS" `
  --reference-filter DJI_06_20260411_165508 DJI_07_20260411_172612 DJI_15_20260411_170802 DJI_16_20260411_173907 MONO-017 `
  -t "D:\2026-04-11 - Casamento - Debora Seimaru e Lucas - Soho\01 CAMERAS" `
  --target-range A7IV_20260411_9715..A7IV_20260411_9729 ZVE10_02_20260411_9450..ZVE10_02_20260411_9453 ZVE10_01_20260411_9647..ZVE10_01_20260411_9651 C0020..C0029
```

Rodar config:

```powershell
python main.py --config "configs\casamento_soho_auto.json"
```

Validar golden:

```powershell
python tools\validate_golden.py
```
