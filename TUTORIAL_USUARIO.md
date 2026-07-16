# Tutorial Para Baixar, Instalar E Usar

Este tutorial e para quem nunca usou GitHub ou Python. A ferramenta gera um
arquivo XML para importar no Adobe Premiere com os videos e audios sincronizados.

## 1. Entrar Na Pagina Do GitHub

1. Abra o navegador.
2. Entre neste link:

```text
https://github.com/Yuki-JP/WaveSync
```

3. A pagina do projeto vai abrir.

## 2. Baixar O Projeto

1. Na pagina do GitHub, clique no botao verde `Code`.
2. Clique em `Download ZIP`.
3. Espere o download terminar.
4. Clique com o botao direito no arquivo `.zip` baixado.
5. Escolha `Extrair tudo...`.
6. Escolha uma pasta facil, por exemplo:

```text
C:\WaveSync
```

7. Entre na pasta extraida.

Importante: nao rode a ferramenta direto de dentro do `.zip`. Primeiro extraia.

## 3. Instalar O Python E As Dependencias

Dentro da pasta extraida do projeto, abra este arquivo:

```text
Instalar_Python39_E_Dependencias.bat
```

Ele vai:

1. verificar se o Python 3.9 ja existe;
2. baixar e instalar Python 3.9 se estiver faltando;
3. criar o ambiente local `.venv`;
4. instalar as dependencias do projeto;
5. abrir a interface.

Se preferir instalar Python manualmente:

1. Abra o navegador.
2. Entre em:

```text
https://www.python.org/downloads/
```

3. Baixe o Python para Windows.
4. Abra o instalador.
5. Marque a opcao `Add python.exe to PATH`.
6. Clique em `Install Now`.
7. Espere terminar.
8. Depois rode `Instalar_Python39_E_Dependencias.bat` para instalar as
   dependencias.

## 4. Abrir A Ferramenta

O arquivo `Instalar_Python39_E_Dependencias.bat` abre a interface no final da
instalacao.

Se a interface for fechada e voce quiser abrir de novo, entre na pasta do
projeto, clique na barra de endereco do Explorer, digite `powershell` e aperte
Enter.

No PowerShell, rode:

```powershell
python tkinter_app.py
```

## 5. Selecionar Os Audios

Na janela da ferramenta:

1. Clique no botao de adicionar audios.
2. Selecione os arquivos das lapelas, mesa de som ou gravadores.
3. Pode selecionar varios arquivos de uma vez.
4. Se escolher algo errado, use os botoes de remover audios.

Exemplos de audio:

```text
DJI_01_20260307_162306.WAV
DJI_02_20260307_162308.WAV
MONO-017.wav
```

## 6. Selecionar Os Videos

1. Clique no botao de adicionar videos.
2. Selecione os arquivos das cameras que deseja sincronizar.
3. Pode selecionar videos de varias cameras.
4. Se escolher algo errado, use os botoes de remover videos.

Exemplos de video:

```text
A7IV_20260411_9715.MP4
ZVE10_02_20260411_9450.MP4
C0020.MP4
```

Dica: selecione somente o que realmente quer sincronizar. Isso deixa o processo
mais rapido e evita trazer arquivos que nao fazem parte daquele bloco.

## 7. Sincronizar

1. Clique em `Sincronizar`.
2. A ferramenta vai perguntar onde salvar o XML.
3. Escolha uma pasta e um nome para o arquivo.
4. Clique em salvar.
5. Aguarde o processamento terminar.

Durante a sincronizacao, a ferramenta pode demorar. Casamentos com muitas
cameras e muitos audios podem levar varios minutos.

## 8. Importar No Premiere

1. Abra o Adobe Premiere.
2. Abra seu projeto.
3. Va em:

```text
Arquivo > Importar
```

4. Selecione o XML gerado pela ferramenta.
5. O Premiere vai criar uma sequencia com os arquivos sincronizados.

## 9. Conferir A Timeline

Depois de importar:

1. De play em trechos com fala.
2. Compare boca, audio das cameras e lapelas.
3. Confira se as cameras estao nas tracks de video.
4. Confira se as lapelas/mesa estao nas tracks de audio mais abaixo.

Se um arquivo nao sincronizar, normalmente ele aparece no resumo como falha ou
fica fora da sequencia esperada.

## 10. Problemas Comuns

### A Interface Nao Abriu

Rode novamente:

```text
Instalar_Python39_E_Dependencias.bat
```

Se preferir abrir manualmente, use `python tkinter_app.py`.

### O Python Nao Foi Encontrado

Instale o Python novamente e marque:

```text
Add python.exe to PATH
```

Depois feche e abra o PowerShell de novo.

### A Primeira Abertura Demorou Muito

Isso e normal. Na primeira abertura, a ferramenta instala as dependencias. Nas
proximas vezes, ela abre mais rapido.

### Quero Rodar Manualmente

Na pasta do projeto, rode:

```powershell
python tkinter_app.py
```

## 11. Resumo Rapido

```text
1. Entrar em https://github.com/Yuki-JP/WaveSync
2. Code > Download ZIP
3. Extrair o ZIP
4. Rodar Instalar_Python39_E_Dependencias.bat
5. Selecionar audios
6. Selecionar videos
7. Clicar em Sincronizar
8. Escolher onde salvar o XML
9. Importar o XML no Premiere
```

## Enviar Diagnostico Para Suporte

Se o suporte pedir um diagnostico, clique em `Enviar diagnostico para suporte`
dentro do WaveSync.

Antes de enviar, o programa mostra uma confirmacao. O pacote enviado inclui logs,
configs, XMLs, CSVs, JSONs e um resumo do sistema. Ele nao envia audios nem
videos do casamento.

Se aparecer a mensagem `Suporte nao configurado`, significa que o arquivo
`support_config.json` ainda nao foi configurado na pasta do WaveSync.
