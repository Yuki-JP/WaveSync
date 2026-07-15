# PluralEyes Clone - Sync Multicamera

Ferramenta Python para sincronizacao automatica de audio/video em fluxos reais
de casamento, com exportacao FCP XML para Adobe Premiere.

## Para Usuario Final

Passo a passo completo:

```text
TUTORIAL_USUARIO.md
```

Fluxo rapido:

1. Baixe o projeto pelo GitHub.
2. Extraia o arquivo `.zip`.
3. Abra `Instalar_Python39_E_Dependencias.bat`.
4. Selecione os audios de referencia.
5. Selecione os videos das cameras.
6. Clique em `Sincronizar`.
7. Escolha onde salvar o XML.
8. Importe o XML no Premiere.

## Arquivos Principais

```text
Instalar_Python39_E_Dependencias.bat  Instala Python 3.9/dependencias e abre a interface
TUTORIAL_USUARIO.md                   Tutorial para usuario leigo
requirements.txt                      Dependencias Python
tkinter_app.py                        Interface grafica local
main.py                               Motor principal de sincronizacao
backend/audio_processor.py            Extracao e features DSP
backend/xml_generator.py              Geracao do XML para Premiere
backend/audit_report.py               Relatorios CSV/JSON
tools/install_python39_deps.ps1       Instalador chamado pelo .bat
tools/make_config.py                  Gerador de config usado pela interface
```

## Como Abrir Manualmente

Se o Python e as dependencias ja estiverem instalados:

```powershell
python tkinter_app.py
```

## Organizacao Das Tracks No Premiere

O XML organiza as cameras por prioridade simples de equipamento:

- cameras melhores ficam nas tracks de video mais altas (`V4`, `V3`, ...);
- audios nativos das cameras ficam primeiro (`A1`, `A2`, ...);
- lapelas e mesa ficam nas tracks de audio abaixo das cameras.

Exemplo com 4 cameras:

- `V4/A1`: A7IV
- `V3/A2`: ZVE10
- `V2/A3`: ZVE10
- `V1/A4`: DJI/Osmo
- `A5+`: lapelas/mesa

## Arquivos Gerados Localmente

Durante o uso, a ferramenta pode criar:

```text
configs/
selections/
temp/
output/
```

Essas pastas guardam configs temporarios, cache DSP, XMLs e auditorias. Elas sao
locais da maquina do usuario e nao precisam ir para o Git.
