"""
Motor de processamento de audio para sincronizacao por correlacao cruzada.

O modulo extrai audio de midias via FFmpeg, calcula offsets entre faixas e usa
metadados do sistema apenas como uma estimativa inicial de janela. Como o
mtime do Windows costuma representar o final da gravacao/copia, a estimativa
de inicio e corrigida subtraindo a duracao real do arquivo.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

import ffmpeg
import librosa
import numpy as np
from scipy.signal import correlate
from tqdm import tqdm


SAMPLE_RATE = 22_050
CHANNELS = 1
SAMPLE_FORMAT = "s16"
DEFAULT_WINDOW_SECONDS = 180.0
LOW_CONFIDENCE_MIN_Z_SCORE = 8.0
LOW_CONFIDENCE_MIN_PROMINENCE = 1.15

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CorrelationPeak:
    """Resultado interno da busca de pico na correlacao."""

    peak_index: int
    lag_samples: int
    offset_seconds: float
    peak_value: float
    z_score: float
    prominence_ratio: float
    low_confidence: bool


def extract_audio_from_media(file_path: str | Path, output_wav_path: str | Path) -> Path:
    """Extrai ou converte a pista de audio de uma midia para WAV mono 16-bit/22050 Hz."""
    input_path = Path(file_path)
    output_path = Path(output_wav_path)

    if not input_path.exists():
        raise FileNotFoundError(f"Arquivo de entrada nao encontrado: {input_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Extraindo audio: %s -> %s", input_path, output_path)

    try:
        import imageio_ffmpeg

        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

        (
            ffmpeg.input(str(input_path))
            .output(
                str(output_path),
                acodec="pcm_s16le",
                ac=CHANNELS,
                ar=SAMPLE_RATE,
                format="wav",
                sample_fmt=SAMPLE_FORMAT,
            )
            .overwrite_output()
            .run(cmd=ffmpeg_exe, capture_stdout=True, capture_stderr=True)
        )
    except ffmpeg.Error as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
        logger.error("Falha ao executar FFmpeg para '%s'.", input_path)
        raise RuntimeError(f"Erro do FFmpeg ao extrair audio de '{input_path}': {stderr}") from exc
    except Exception as exc:
        logger.exception("Falha inesperada ao extrair audio de '%s'.", input_path)
        raise RuntimeError(f"Falha inesperada ao extrair audio de '{input_path}': {exc}") from exc

    logger.info("Audio extraido com sucesso: %s", output_path)
    return output_path


def calculate_offset(reference_wav: str | Path, target_wav: str | Path) -> float:
    """Calcula o offset usando correlacao total, sem restringir por metadados."""
    return calculate_offset_with_window(
        reference_wav=reference_wav,
        target_wav=target_wav,
        estimated_offset=None,
        window_seconds=None,
    )


def calculate_offset_with_window(
    reference_wav: str | Path,
    target_wav: str | Path,
    estimated_offset: float | None,
    window_seconds: float | None = DEFAULT_WINDOW_SECONDS,
) -> float:
    """
    Calcula o offset do target em relacao a referencia.

    Quando uma estimativa e uma janela sao fornecidas, a primeira busca ocorre
    em torno dessa regiao. Se o pico local tiver baixa confianca, o algoritmo
    expande para correlacao total e escolhe o pico absoluto mais proeminente.

    Offset positivo significa que o target comeca depois da referencia.
    Offset negativo significa que o target esta adiantado em relacao a referencia.
    """
    reference_signal, target_signal, sample_rate = _load_and_prepare_pair(
        reference_wav,
        target_wav,
    )

    logger.info("Calculando correlacao cruzada...")
    correlation = _correlate_signals(reference_signal, target_signal)
    center_index = len(reference_signal) - 1

    if estimated_offset is None or window_seconds is None:
        peak = _find_peak(correlation, center_index, sample_rate)
        logger.info("Offset encontrado por busca total: %.6f segundos", peak.offset_seconds)
        return peak.offset_seconds

    expected_peak_index = center_index + int(round(estimated_offset * sample_rate))
    window_samples = int(round(window_seconds * sample_rate))
    start_search = max(0, expected_peak_index - window_samples)
    end_search = min(len(correlation), expected_peak_index + window_samples + 1)

    logger.info(
        "Buscando em janela: estimativa=%.3fs, janela=+/-%.3fs",
        estimated_offset,
        window_seconds,
    )

    if start_search >= end_search:
        logger.warning("Janela estimada invalida. Expandindo para busca total.")
        peak = _find_peak(correlation, center_index, sample_rate)
        logger.info("Offset encontrado por busca total: %.6f segundos", peak.offset_seconds)
        return peak.offset_seconds

    window_peak = _find_peak(
        correlation,
        center_index,
        sample_rate,
        start_index=start_search,
        end_index=end_search,
    )

    logger.info(
        "Pico na janela: offset=%.6fs, z=%.2f, proeminencia=%.3f",
        window_peak.offset_seconds,
        window_peak.z_score,
        window_peak.prominence_ratio,
    )

    if not window_peak.low_confidence:
        logger.info("Offset encontrado na janela: %.6f segundos", window_peak.offset_seconds)
        return window_peak.offset_seconds

    logger.warning(
        "Pico na janela com baixa confianca. Expandindo para correlacao total."
    )
    full_peak = _find_peak(correlation, center_index, sample_rate)

    logger.info(
        "Pico total: offset=%.6fs, z=%.2f, proeminencia=%.3f",
        full_peak.offset_seconds,
        full_peak.z_score,
        full_peak.prominence_ratio,
    )

    if _is_better_fallback(full_peak, window_peak):
        logger.info("Fallback aceito. Offset encontrado: %.6f segundos", full_peak.offset_seconds)
        return full_peak.offset_seconds

    logger.info(
        "Fallback nao superou a janela com clareza. Mantendo offset local: %.6f segundos",
        window_peak.offset_seconds,
    )
    return window_peak.offset_seconds


def sync_multiple_tracks(reference_file: str | Path, target_files_list: list[str | Path]) -> dict:
    """
    Sincroniza multiplos arquivos usando o inicio estimado da gravacao.

    O Windows mtime normalmente aponta para o fim da escrita do arquivo. Para
    aproximar o inicio real da gravacao, usamos:

        inicio_estimado = os.path.getmtime(arquivo_original) - duracao
    """
    logger.info("Iniciando sincronizacao multi-track inteligente...")

    ref_path = Path(reference_file)
    if not ref_path.exists():
        raise FileNotFoundError(f"Arquivo de referencia nao encontrado: {ref_path}")

    ref_wav = Path("temp/main_reference.wav")
    extract_audio_from_media(ref_path, ref_wav)

    ref_duration = get_audio_duration_seconds(ref_wav)
    ref_start_time = get_estimated_recording_start_time(ref_path, ref_duration)

    logger.info(
        "Referencia: duracao=%.3fs, mtime=%.3f, inicio_estimado=%.3f",
        ref_duration,
        os.path.getmtime(ref_path),
        ref_start_time,
    )

    results = {
        "reference": str(reference_file),
        "offsets": {},
        "metadata": {
            "reference_duration_seconds": ref_duration,
            "reference_estimated_start_time": ref_start_time,
        },
    }

    for index, target_file in enumerate(tqdm(target_files_list, desc="Sincronizando faixas")):
        try:
            target_path = Path(target_file)
            if not target_path.exists():
                raise FileNotFoundError(f"Arquivo alvo nao encontrado: {target_path}")

            target_wav = Path(f"temp/target_{index}_{target_path.stem}.wav")
            extract_audio_from_media(target_path, target_wav)

            target_duration = get_audio_duration_seconds(target_wav)
            target_start_time = get_estimated_recording_start_time(target_path, target_duration)
            estimated_offset = target_start_time - ref_start_time

            logger.info(
                "Alvo %s: duracao=%.3fs, mtime=%.3f, inicio_estimado=%.3f, offset_estimado=%.3fs",
                target_path.name,
                target_duration,
                os.path.getmtime(target_path),
                target_start_time,
                estimated_offset,
            )

            offset = calculate_offset_with_window(
                ref_wav,
                target_wav,
                estimated_offset=estimated_offset,
                window_seconds=DEFAULT_WINDOW_SECONDS,
            )

            results["offsets"][str(target_file)] = offset
            results["metadata"][str(target_file)] = {
                "duration_seconds": target_duration,
                "estimated_start_time": target_start_time,
                "estimated_offset_seconds": estimated_offset,
            }
        except Exception as exc:
            logger.error("Erro ao sincronizar o arquivo %s: %s", target_file, exc)
            results["offsets"][str(target_file)] = None

    return results


def get_audio_duration_seconds(audio_path: str | Path) -> float:
    """Retorna a duracao do audio em segundos usando librosa."""
    path = Path(audio_path)
    if not path.exists():
        raise FileNotFoundError(f"Arquivo de audio nao encontrado: {path}")

    try:
        duration = float(librosa.get_duration(path=str(path)))
    except Exception as exc:
        raise RuntimeError(f"Erro ao obter duracao de '{path}'.") from exc

    if duration <= 0.0:
        raise ValueError(f"Duracao invalida para '{path}': {duration}")

    return duration


def get_estimated_recording_start_time(file_path: str | Path, duration_seconds: float) -> float:
    """Estima o inicio real da gravacao a partir do mtime menos a duracao."""
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Arquivo nao encontrado: {path}")
    if duration_seconds <= 0.0:
        raise ValueError(f"Duracao invalida para estimativa de inicio: {duration_seconds}")

    return os.path.getmtime(path) - duration_seconds


def _load_and_prepare_pair(
    reference_wav: str | Path,
    target_wav: str | Path,
) -> tuple[np.ndarray, np.ndarray, int]:
    reference_path = Path(reference_wav)
    target_path = Path(target_wav)

    if not reference_path.exists():
        raise FileNotFoundError(f"WAV de referencia nao encontrado: {reference_path}")
    if not target_path.exists():
        raise FileNotFoundError(f"WAV alvo nao encontrado: {target_path}")

    logger.info("Carregando audios em %s Hz...", SAMPLE_RATE)

    try:
        reference_signal, sr_reference = librosa.load(reference_path, sr=SAMPLE_RATE, mono=True)
        target_signal, sr_target = librosa.load(target_path, sr=SAMPLE_RATE, mono=True)
    except Exception as exc:
        logger.exception("Falha ao carregar arquivos WAV.")
        raise RuntimeError("Erro ao carregar arquivos WAV.") from exc

    if sr_reference != sr_target:
        raise ValueError(
            f"Taxas de amostragem diferentes: referencia={sr_reference}, target={sr_target}"
        )

    return (
        _prepare_signal(reference_signal, "referencia"),
        _prepare_signal(target_signal, "target"),
        int(sr_reference),
    )


def _prepare_signal(signal: np.ndarray, label: str) -> np.ndarray:
    """Remove DC offset e normaliza amplitude para tornar a correlacao mais estavel."""
    if signal.size == 0:
        raise ValueError(f"O audio de {label} esta vazio.")

    prepared = np.asarray(signal, dtype=np.float32)
    prepared = prepared - np.mean(prepared)

    peak = float(np.max(np.abs(prepared)))
    if peak == 0.0:
        raise ValueError(f"O audio de {label} nao possui sinal util.")

    return prepared / peak


def _correlate_signals(reference_signal: np.ndarray, target_signal: np.ndarray) -> np.ndarray:
    try:
        return correlate(target_signal, reference_signal, mode="full", method="fft")
    except Exception as exc:
        logger.exception("Falha ao calcular correlacao cruzada.")
        raise RuntimeError("Erro ao calcular correlacao cruzada.") from exc


def _find_peak(
    correlation: np.ndarray,
    center_index: int,
    sample_rate: int,
    start_index: int = 0,
    end_index: int | None = None,
) -> CorrelationPeak:
    end = len(correlation) if end_index is None else end_index
    if start_index < 0 or end > len(correlation) or start_index >= end:
        raise ValueError(
            f"Intervalo de busca invalido: start={start_index}, end={end}, len={len(correlation)}"
        )

    segment = correlation[start_index:end]
    local_peak_index = int(np.argmax(segment))
    peak_index = start_index + local_peak_index
    peak_value = float(segment[local_peak_index])

    # CORREÇÃO: Média e Desvio Padrão agora utilizam a matriz GLOBAL da correlação
    # para evitar a inflação estatística artificial em sub-segmentos pequenos.
    global_mean = float(np.mean(correlation))
    global_std = float(np.std(correlation))
    z_score = (peak_value - global_mean) / global_std if global_std > 0.0 else 0.0

    global_percentile_95 = float(np.percentile(correlation, 95))
    prominence_denominator = max(abs(global_percentile_95), 1e-12)
    prominence_ratio = peak_value / prominence_denominator

    low_confidence = (
        z_score < LOW_CONFIDENCE_MIN_Z_SCORE
        or prominence_ratio < LOW_CONFIDENCE_MIN_PROMINENCE
    )

    lag_samples = peak_index - center_index
    offset_seconds = lag_samples / float(sample_rate)

    return CorrelationPeak(
        peak_index=peak_index,
        lag_samples=lag_samples,
        offset_seconds=float(offset_seconds),
        peak_value=peak_value,
        z_score=z_score,
        prominence_ratio=prominence_ratio,
        low_confidence=low_confidence,
    )


def _is_better_fallback(full_peak: CorrelationPeak, window_peak: CorrelationPeak) -> bool:
    """Aceita fallback quando o pico global e claramente mais confiavel ou expressivo."""
    if full_peak.peak_value <= 0.0:
        return False

    peak_gain = full_peak.peak_value / max(window_peak.peak_value, 1e-12)
    z_gain = full_peak.z_score - window_peak.z_score
    
    # Se o pico total for 25% mais alto e apresentar melhoria de Z-Score, aceita o fallback
    return peak_gain >= 1.25 and z_gain >= 3.0


def _format_seconds_as_minutes(offset_seconds: float) -> str:
    sign = "+" if offset_seconds >= 0.0 else "-"
    absolute = abs(offset_seconds)
    minutes = int(absolute // 60)
    seconds = absolute % 60
    return f"{sign}{minutes:02d}min {seconds:06.3f}s"


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    # Caminho local do áudio de lapela (Referência)
    audio_lapela = (
        r"E:\2026-05-16 - Casamento - Paulinho Pai e Tereza - Cananeia"
        r"\02 AUDIOS\LAPELA 02 - DJI MIC - AMARELO\DJI_11_20260516_105338.WAV"
    )

    # Lista de vídeos das câmeras
    videos_da_camera = [
        (
            r"E:\2026-05-16 - Casamento - Paulinho Pai e Tereza - Cananeia"
            r"\01 CAMERAS\CAM 01 - A7III\C0012.MP4"
        ),
        (
            r"E:\2026-05-16 - Casamento - Paulinho Pai e Tereza - Cananeia"
            r"\01 CAMERAS\CAM 01 - A7III\C0013.MP4"
        ),
        (
            r"E:\2026-05-16 - Casamento - Paulinho Pai e Tereza - Cananeia"
            r"\01 CAMERAS\CAM 02 - A6500\C0030.MP4"
        ),
    ]

    resultado_sync = sync_multiple_tracks(audio_lapela, videos_da_camera)

    print("\n" + "=" * 56)
    print("RESULTADO DA SINCRONIZACAO RE-CALIBRADA")
    print(f"Referencia: {resultado_sync['reference']}\n")

    for video, offset in resultado_sync["offsets"].items():
        if offset is not None:
            print(
                f"-> {Path(video).name}: "
                f"{_format_seconds_as_minutes(offset)} ({offset:.6f} s)"
            )
        else:
            print(f"-> {Path(video).name}: falha na sincronizacao")

    print("=" * 56)