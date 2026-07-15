"""
Processamento de audio para sincronizacao por DSP.

Este modulo cuida apenas da etapa de audio:
- extrair qualquer midia para WAV mono em 11025 Hz;
- medir duracao real pelo numero de samples do WAV extraido;
- gerar features de saliencia baseadas em transientes/ataques de voz;
- normalizar a saliencia por Z-score para correlacao posterior.
"""

from __future__ import annotations

import logging
import hashlib
import json
import re
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np


SAMPLE_RATE = 11_025
CHANNELS = 1
WAV_CODEC = "pcm_s16le"
TRANSIENT_DECIMATION_FACTOR = 5
TRANSIENT_FEATURE_RATE = SAMPLE_RATE / TRANSIENT_DECIMATION_FACTOR
EPSILON = 1e-12
DEFAULT_WINDOW_SECONDS = 300.0
CACHE_VERSION = 1

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AudioFeatures:
    """
    Representacao DSP usada pelas proximas etapas de alinhamento.

    Os nomes `envelope` e `normalized_envelope` sao preservados por contrato
    com o main.py, mas agora carregam saliencia de transientes, nao RMS.
    """

    wav_path: Path
    duration_seconds: float
    sample_rate: int
    feature_hop_samples: int
    salience: np.ndarray
    normalized_salience: np.ndarray

    @property
    def feature_rate(self) -> float:
        return self.sample_rate / float(self.feature_hop_samples)

    @property
    def envelope(self) -> np.ndarray:
        return self.salience

    @property
    def normalized_envelope(self) -> np.ndarray:
        return self.normalized_salience

    @property
    def rms_window_samples(self) -> int:
        return 1

    @property
    def rms_hop_samples(self) -> int:
        return self.feature_hop_samples


@dataclass(frozen=True)
class CorrelationPeak:
    """Pico encontrado na correlacao entre duas features normalizadas."""

    peak_index: int
    lag_samples: int
    offset_seconds: float
    peak_value: float
    z_score: float
    prominence_ratio: float
    low_confidence: bool


@dataclass(frozen=True)
class OffsetCalculation:
    """Resultado de compatibilidade para a etapa de alinhamento."""

    offset_seconds: float
    peak: CorrelationPeak
    source: str


@dataclass(frozen=True)
class SpanningGroup:
    """Grupo simples de arquivos sequenciais, mantido para compatibilidade."""

    group_id: str
    paths: tuple[str, ...]


@dataclass(frozen=True)
class CachedAudioPreparation:
    """Resultado de preparo de audio com cache de WAV e features."""

    wav_path: Path
    features: AudioFeatures
    cache_hit_wav: bool
    cache_hit_features: bool


def extract_audio_from_media(file_path: str | Path, output_wav_path: str | Path) -> Path:
    """
    Extrai audio de uma midia para WAV mono/11025 Hz usando FFmpeg simples.

    A duracao oficial nao vem do container original; ela deve ser medida depois
    no WAV gerado por `get_wav_duration_seconds`.
    """
    input_path = Path(file_path)
    output_path = Path(output_wav_path)

    if not input_path.exists():
        raise FileNotFoundError(f"Arquivo de entrada nao encontrado: {input_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Extraindo audio: %s -> %s", input_path, output_path)

    ffmpeg_exe = resolve_ffmpeg_executable()
    command = [
        ffmpeg_exe,
        "-y",
        "-i",
        str(input_path),
        "-vn",
        "-acodec",
        WAV_CODEC,
        "-ac",
        str(CHANNELS),
        "-ar",
        str(SAMPLE_RATE),
        "-f",
        "wav",
        str(output_path),
    ]

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception as exc:
        raise RuntimeError(f"Falha ao extrair audio de '{input_path}': {exc}") from exc

    if completed.returncode != 0:
        raise RuntimeError(
            f"Erro do FFmpeg ao extrair audio de '{input_path}': {completed.stderr}"
        )

    return output_path


def resolve_ffmpeg_executable() -> str:
    """Retorna o executavel do FFmpeg empacotado quando disponivel."""
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def prepare_cached_audio_features(
    media_path: str | Path,
    cache_root: str | Path,
    *,
    label: str = "media",
) -> CachedAudioPreparation:
    """
    Extrai WAV e calcula features usando cache por assinatura do arquivo fonte.

    A assinatura inclui caminho resolvido, tamanho, mtime_ns e parametros DSP.
    Se o arquivo ou os parametros mudarem, um novo diretorio de cache e criado.
    """
    source_path = Path(media_path)
    if not source_path.exists():
        raise FileNotFoundError(f"Arquivo de entrada nao encontrado: {source_path}")

    cache_dir = cache_directory_for_media(source_path, cache_root)
    cache_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = cache_dir / "metadata.json"
    wav_path = cache_dir / f"{safe_cache_label(label)}.wav"
    features_path = cache_dir / "features.npz"

    signature = media_cache_signature(source_path)
    cache_metadata = read_cache_metadata(metadata_path)
    metadata_matches = cache_metadata.get("signature") == signature

    cache_hit_wav = metadata_matches and is_valid_cached_wav(wav_path)
    if cache_hit_wav:
        logger.info("Cache WAV: %s -> %s", source_path.name, wav_path)
    elif can_reuse_source_wav(source_path):
        logger.info("Reutilizando WAV compativel: %s -> %s", source_path, wav_path)
        copy_binary_file(source_path, wav_path)
    else:
        extract_audio_from_media(source_path, wav_path)

    cache_hit_features = metadata_matches and is_valid_cached_features(features_path)
    if cache_hit_features:
        logger.info("Cache features: %s", source_path.name)
        try:
            features = load_cached_audio_features(wav_path, features_path)
        except (OSError, ValueError) as exc:
            logger.warning(
                "Cache features invalido para %s: %s. Recalculando.",
                source_path.name,
                exc,
            )
            cache_hit_features = False
            features = build_audio_features(wav_path)
            save_cached_audio_features(features, features_path)
    else:
        features = build_audio_features(wav_path)
        save_cached_audio_features(features, features_path)

    write_cache_metadata(
        metadata_path,
        {
            "signature": signature,
            "source_path": str(source_path.resolve()),
            "wav_path": str(wav_path),
            "features_path": str(features_path),
            "duration_seconds": features.duration_seconds,
            "sample_rate": features.sample_rate,
            "feature_hop_samples": features.feature_hop_samples,
        },
    )

    return CachedAudioPreparation(
        wav_path=wav_path,
        features=features,
        cache_hit_wav=cache_hit_wav,
        cache_hit_features=cache_hit_features,
    )


def cache_directory_for_media(media_path: Path, cache_root: str | Path) -> Path:
    return Path(cache_root) / media_cache_key(media_path)


def media_cache_key(media_path: Path) -> str:
    signature = media_cache_signature(media_path)
    encoded = json.dumps(signature, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


def media_cache_signature(media_path: Path) -> dict:
    resolved = media_path.resolve()
    stat_result = resolved.stat()
    return {
        "cache_version": CACHE_VERSION,
        "path": str(resolved),
        "size": stat_result.st_size,
        "mtime_ns": stat_result.st_mtime_ns,
        "sample_rate": SAMPLE_RATE,
        "channels": CHANNELS,
        "wav_codec": WAV_CODEC,
        "transient_decimation_factor": TRANSIENT_DECIMATION_FACTOR,
    }


def read_cache_metadata(metadata_path: Path) -> dict:
    if not metadata_path.exists():
        return {}
    try:
        return json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_cache_metadata(metadata_path: Path, metadata: dict) -> None:
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def is_valid_cached_wav(wav_path: Path) -> bool:
    if not wav_path.exists():
        return False
    try:
        with wave.open(str(wav_path), "rb") as wav_file:
            return (
                wav_file.getnchannels() == CHANNELS
                and wav_file.getframerate() == SAMPLE_RATE
                and wav_file.getnframes() > 0
            )
    except (OSError, wave.Error):
        return False


def can_reuse_source_wav(source_path: Path) -> bool:
    if source_path.suffix.casefold() != ".wav":
        return False
    return is_valid_cached_wav(source_path)


def copy_binary_file(source_path: Path, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with source_path.open("rb") as source_file, output_path.open("wb") as output_file:
        while True:
            chunk = source_file.read(1024 * 1024)
            if not chunk:
                break
            output_file.write(chunk)


def is_valid_cached_features(features_path: Path) -> bool:
    if not features_path.exists():
        return False
    try:
        with np.load(features_path) as data:
            required = {"salience", "normalized_salience", "duration_seconds"}
            return required.issubset(data.files)
    except (OSError, ValueError):
        return False


def save_cached_audio_features(features: AudioFeatures, features_path: Path) -> None:
    np.savez_compressed(
        features_path,
        salience=features.salience.astype(np.float32),
        normalized_salience=features.normalized_salience.astype(np.float32),
        duration_seconds=np.array(features.duration_seconds, dtype=np.float64),
        sample_rate=np.array(features.sample_rate, dtype=np.int32),
        feature_hop_samples=np.array(features.feature_hop_samples, dtype=np.int32),
    )


def load_cached_audio_features(wav_path: Path, features_path: Path) -> AudioFeatures:
    with np.load(features_path) as data:
        salience = np.asarray(data["salience"], dtype=np.float32)
        normalized_salience = np.asarray(data["normalized_salience"], dtype=np.float32)
        duration_seconds = float(data["duration_seconds"])
        sample_rate = int(data["sample_rate"]) if "sample_rate" in data.files else SAMPLE_RATE
        feature_hop_samples = (
            int(data["feature_hop_samples"])
            if "feature_hop_samples" in data.files
            else TRANSIENT_DECIMATION_FACTOR
        )

    if sample_rate != SAMPLE_RATE:
        raise ValueError(f"Feature cache com sample_rate invalido: {sample_rate}")
    if feature_hop_samples != TRANSIENT_DECIMATION_FACTOR:
        raise ValueError(
            f"Feature cache com hop invalido: {feature_hop_samples}"
        )
    if salience.size == 0 or normalized_salience.size == 0:
        raise ValueError("Feature cache vazio.")

    return AudioFeatures(
        wav_path=wav_path,
        duration_seconds=duration_seconds,
        sample_rate=sample_rate,
        feature_hop_samples=feature_hop_samples,
        salience=salience,
        normalized_salience=normalized_salience,
    )


def safe_cache_label(label: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", label.strip())
    return cleaned.strip("._") or "media"


def get_wav_duration_seconds(wav_path: str | Path) -> float:
    """Calcula a duracao real do WAV por quantidade de samples / sample rate."""
    path = Path(wav_path)
    if not path.exists():
        raise FileNotFoundError(f"WAV nao encontrado: {path}")

    try:
        with wave.open(str(path), "rb") as wav_file:
            frame_count = wav_file.getnframes()
            sample_rate = wav_file.getframerate()
    except wave.Error as exc:
        raise RuntimeError(f"Erro ao ler WAV '{path}'.") from exc

    if frame_count <= 0:
        raise ValueError(f"WAV sem samples: {path}")
    if sample_rate <= 0:
        raise ValueError(f"Sample rate invalido no WAV '{path}': {sample_rate}")

    return frame_count / float(sample_rate)


def get_audio_duration_seconds(audio_path: str | Path) -> float:
    """
    Retorna duracao por samples.

    Nesta etapa, a fonte confiavel e o WAV extraido. Arquivos nao-WAV devem ser
    passados antes por `extract_audio_from_media`.
    """
    path = Path(audio_path)
    if path.suffix.lower() != ".wav":
        raise ValueError(
            "Duracao oficial deve ser medida no WAV extraido, nao no container original."
        )
    return get_wav_duration_seconds(path)


def load_audio_samples(wav_path: str | Path, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Carrega audio mono em float32 na taxa fixa do projeto."""
    path = Path(wav_path)
    if not path.exists():
        raise FileNotFoundError(f"WAV nao encontrado: {path}")

    try:
        with wave.open(str(path), "rb") as wav_file:
            channels = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
            loaded_sample_rate = wav_file.getframerate()
            frame_count = wav_file.getnframes()
            raw_audio = wav_file.readframes(frame_count)
    except wave.Error as exc:
        raise RuntimeError(f"Erro ao carregar WAV '{path}'.") from exc

    if loaded_sample_rate != sample_rate:
        raise ValueError(
            f"Sample rate inesperado em '{path}': {loaded_sample_rate} Hz, esperado {sample_rate} Hz"
        )

    samples = decode_pcm_samples(raw_audio, sample_width, channels)
    if samples.size == 0:
        raise ValueError(f"Audio vazio: {path}")

    return np.asarray(samples, dtype=np.float32)


def decode_pcm_samples(raw_audio: bytes, sample_width: int, channels: int) -> np.ndarray:
    """Converte PCM WAV em float32 mono normalizado para aproximadamente [-1, 1]."""
    if sample_width == 1:
        samples = (np.frombuffer(raw_audio, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif sample_width == 2:
        samples = np.frombuffer(raw_audio, dtype="<i2").astype(np.float32) / 32768.0
    elif sample_width == 4:
        samples = np.frombuffer(raw_audio, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"Profundidade PCM nao suportada: {sample_width} byte(s)")

    if channels > 1:
        usable_size = (samples.size // channels) * channels
        samples = samples[:usable_size].reshape(-1, channels).mean(axis=1)

    return samples.astype(np.float32)


def calculate_transient_salience(
    samples: np.ndarray,
    decimation_factor: int = TRANSIENT_DECIMATION_FACTOR,
) -> np.ndarray:
    """
    Calcula uma feature de saliencia baseada em ataques/transientes.

    Em vez de energia RMS suavizada, usamos diferenciacao do sinal para destacar
    consoantes, estalos e mudancas rapidas da fala. A magnitude da derivada
    torna a feature robusta a inversoes de polaridade entre microfones/cameras.
    A reducao por max-pooling em blocos pequenos preserva ataques sem calcular
    media de volume pesada.
    """
    audio = np.asarray(samples, dtype=np.float32)
    if audio.size == 0:
        raise ValueError("Nao e possivel calcular saliencia de audio vazio.")

    if decimation_factor <= 0:
        raise ValueError(f"Fator de decimacao invalido: {decimation_factor}")

    audio = audio - np.mean(audio)
    differentiated = np.diff(audio, prepend=audio[0])
    salience = np.abs(differentiated).astype(np.float32)

    if decimation_factor > 1:
        salience = max_pool_1d(salience, decimation_factor)

    if salience.size == 0:
        raise ValueError("Feature de saliencia vazia.")

    return np.asarray(salience, dtype=np.float32)


def max_pool_1d(values: np.ndarray, block_size: int) -> np.ndarray:
    """Reduz taxa preservando o maior ataque dentro de cada bloco curto."""
    array = np.asarray(values, dtype=np.float32)
    if block_size <= 1:
        return array

    usable_size = (array.size // block_size) * block_size
    if usable_size == 0:
        return array

    pooled = array[:usable_size].reshape(-1, block_size).max(axis=1)
    if usable_size < array.size:
        tail = np.max(array[usable_size:])
        pooled = np.append(pooled, tail)

    return pooled.astype(np.float32)


def zscore_normalize(values: np.ndarray) -> np.ndarray:
    """Aplica normalizacao Z-score: (valor - media) / desvio padrao."""
    array = np.asarray(values, dtype=np.float32)
    if array.size == 0:
        raise ValueError("Nao e possivel normalizar vetor vazio.")

    mean = float(np.mean(array))
    std = float(np.std(array))
    if std <= EPSILON:
        raise ValueError("Feature sem variacao suficiente para normalizacao Z-score.")

    return ((array - mean) / std).astype(np.float32)


def build_audio_features(wav_path: str | Path) -> AudioFeatures:
    """Extrai duracao e saliencia de transientes normalizada de um WAV temporario."""
    path = Path(wav_path)
    samples = load_audio_samples(path)
    salience = calculate_transient_salience(samples)
    normalized_salience = zscore_normalize(salience)

    return AudioFeatures(
        wav_path=path,
        duration_seconds=get_wav_duration_seconds(path),
        sample_rate=SAMPLE_RATE,
        feature_hop_samples=TRANSIENT_DECIMATION_FACTOR,
        salience=salience,
        normalized_salience=normalized_salience,
    )


def seconds_to_samples(seconds: float, sample_rate: int = SAMPLE_RATE) -> int:
    """Converte segundos para samples, garantindo pelo menos 1 sample."""
    if seconds <= 0:
        raise ValueError(f"Duracao de janela invalida: {seconds}")
    return max(1, int(round(seconds * sample_rate)))


def calculate_offset(reference_wav: str | Path, target_wav: str | Path) -> float:
    """Compatibilidade: calcula offset por correlacao total das features Z-score."""
    return calculate_offset_details_with_window(
        reference_wav,
        target_wav,
        estimated_offset=None,
        window_seconds=None,
    ).offset_seconds


def calculate_offset_with_window(
    reference_wav: str | Path,
    target_wav: str | Path,
    estimated_offset: float | None,
    window_seconds: float | None = DEFAULT_WINDOW_SECONDS,
    return_absolute: bool = False,
) -> float:
    """Compatibilidade: retorna apenas o offset em segundos."""
    calculation = calculate_offset_details_with_window(
        reference_wav,
        target_wav,
        estimated_offset=estimated_offset,
        window_seconds=window_seconds,
        return_absolute=return_absolute,
    )
    return calculation.offset_seconds


def calculate_offset_details_with_window(
    reference_wav: str | Path,
    target_wav: str | Path,
    estimated_offset: float | None,
    window_seconds: float | None = DEFAULT_WINDOW_SECONDS,
    return_absolute: bool = False,
) -> OffsetCalculation:
    """
    Calcula offset usando correlacao das features de transientes normalizadas.

    A janela e apenas um recorte opcional ao redor da estimativa recebida; as
    travas cronologicas pertencem a outra etapa do pipeline.
    """
    reference = build_audio_features(reference_wav)
    target = build_audio_features(target_wav)
    feature_rate = reference.feature_rate

    correlation = _fft_correlate_full(target.normalized_envelope, reference.normalized_envelope)
    center_index = len(reference.normalized_envelope) - 1

    source = "full"
    start_index = 0
    end_index = len(correlation)

    if estimated_offset is not None and window_seconds is not None:
        expected_peak = center_index + int(round(estimated_offset * feature_rate))
        window_samples = int(round(window_seconds * feature_rate))
        start_index = max(0, expected_peak - window_samples)
        end_index = min(len(correlation), expected_peak + window_samples + 1)
        source = "window"

    peak = _find_peak(correlation, center_index, feature_rate, start_index, end_index)
    offset = abs(peak.offset_seconds) if return_absolute else peak.offset_seconds
    return OffsetCalculation(offset_seconds=float(offset), peak=peak, source=source)


def detect_spanning_groups(
    target_files_list: list[str | Path],
    metadata: dict,
    max_gap_seconds: float = 3.0,
) -> list[SpanningGroup]:
    """
    Compatibilidade leve para o main.py: detecta nomes sequenciais na mesma pasta.

    A decisao final de como usar esses grupos fica fora deste modulo de DSP.
    """
    _ = metadata, max_gap_seconds
    groups: list[SpanningGroup] = []
    current: list[Path] = []
    previous_parent: Path | None = None
    previous_sequence: tuple[str, int] | None = None

    for raw_path in sorted((Path(item) for item in target_files_list), key=lambda item: str(item).lower()):
        sequence = _parse_sequential_name(raw_path.stem)
        if sequence is None:
            if len(current) > 1:
                groups.append(_build_spanning_group(current, len(groups) + 1))
            current = []
            previous_parent = None
            previous_sequence = None
            continue

        prefix, number = sequence
        continues = (
            current
            and previous_parent == raw_path.parent
            and previous_sequence is not None
            and previous_sequence[0] == prefix
            and previous_sequence[1] + 1 == number
        )

        if continues:
            current.append(raw_path)
        else:
            if len(current) > 1:
                groups.append(_build_spanning_group(current, len(groups) + 1))
            current = [raw_path]

        previous_parent = raw_path.parent
        previous_sequence = sequence

    if len(current) > 1:
        groups.append(_build_spanning_group(current, len(groups) + 1))

    return groups


def _find_peak(
    correlation: np.ndarray,
    center_index: int,
    feature_rate: float,
    start_index: int,
    end_index: int,
) -> CorrelationPeak:
    if start_index < 0 or end_index > len(correlation) or start_index >= end_index:
        raise ValueError(
            f"Janela de correlacao invalida: start={start_index}, end={end_index}"
        )

    segment = correlation[start_index:end_index]
    local_peak_index = int(np.argmax(segment))
    peak_index = start_index + local_peak_index
    peak_value = float(segment[local_peak_index])

    correlation_mean = float(np.mean(correlation))
    correlation_std = float(np.std(correlation))
    z_score = (
        (peak_value - correlation_mean) / correlation_std
        if correlation_std > EPSILON
        else 0.0
    )

    percentile_95 = float(np.percentile(correlation, 95))
    prominence_ratio = peak_value / max(abs(percentile_95), EPSILON)
    lag_samples = peak_index - center_index
    offset_seconds = lag_samples / float(feature_rate)

    return CorrelationPeak(
        peak_index=peak_index,
        lag_samples=lag_samples,
        offset_seconds=float(offset_seconds),
        peak_value=peak_value,
        z_score=float(z_score),
        prominence_ratio=float(prominence_ratio),
        low_confidence=z_score < 8.0 or prominence_ratio < 1.15,
    )


def _fft_correlate_full(signal: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    """Correlacao 1D completa via FFT, sem depender de scipy."""
    left = np.asarray(signal, dtype=np.float32)
    right = np.asarray(kernel, dtype=np.float32)
    if left.size == 0 or right.size == 0:
        raise ValueError("Nao e possivel correlacionar features vazias.")

    full_size = left.size + right.size - 1
    fft_size = 1 << (full_size - 1).bit_length()
    spectrum_left = np.fft.rfft(left, fft_size)
    spectrum_right = np.fft.rfft(right[::-1], fft_size)
    return np.fft.irfft(spectrum_left * spectrum_right, fft_size)[:full_size]


def _parse_sequential_name(stem: str) -> tuple[str, int] | None:
    match = re.search(r"(\d+)$", stem)
    if match is None:
        return None

    prefix = stem[: match.start(1)]
    number_text = match.group(1)
    if not prefix or len(number_text) < 2:
        return None

    return prefix, int(number_text)


def _build_spanning_group(paths: list[Path], group_index: int) -> SpanningGroup:
    first = paths[0]
    last = paths[-1]
    return SpanningGroup(
        group_id=f"span-{group_index:03d}-{first.stem}-to-{last.stem}",
        paths=tuple(str(path) for path in paths),
    )
