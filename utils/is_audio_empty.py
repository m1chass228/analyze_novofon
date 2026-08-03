import subprocess
import re
import logging

logger = logging.getLogger("watchdog")

def is_audio_empty(audio_bytes: bytes, silence_threshold_db: int = -45, min_silence_duration: float = 0.5) -> bool:
    """
    Проверяет, является ли аудиозапись пустой (тишина/гудки), используя CLI утилиту ffmpeg.
    
    :param audio_bytes: Бинарные данные аудиофайла.
    :param silence_threshold_db: Порог тишины в дБ (например, -45).
    :param min_silence_duration: Минимальная длительность тишины в секундах.
    :return: True, если в записи только тишина, иначе False.
    """
    if not audio_bytes:
        logger.warning("Получены пустые байты аудио для проверки через ffmpeg.")
        return True

    # Команда для анализа потока байт без сохранения на диск
    cmd = [
        'ffmpeg',
        '-i', 'pipe:0',
        '-af', f'silencedetect=noise={silence_threshold_db}dB:d={min_silence_duration}',
        '-f', 'null',
        '-'
    ]

    try:
        # Передаем байты напрямую в stdin процесса ffmpeg
        process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        _, stderr_data = process.communicate(input=audio_bytes)
        
        log_output = stderr_data.decode('utf-8', errors='ignore')

        # 1. Вытаскиваем общую длительность аудио из логов ffmpeg
        duration_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", log_output)
        if not duration_match:
            logger.warning("ffmpeg не смог определить длительность файла (возможно, поврежден заголовок).")
            return False

        hours, minutes, seconds = map(float, duration_match.groups())
        total_duration = hours * 3600 + minutes * 60 + seconds

        if total_duration < 0.2:
            logger.debug("Аудиофайл слишком короткий (меньше 200 мс).")
            return True

        # 2. Ищем метки тишины
        silence_starts = [float(m) for m in re.findall(r"silence_start:\s*([\d\.]+)", log_output)]
        silence_ends = [float(m) for m in re.findall(r"silence_end:\s*([\d\.]+)", log_output)]
        silence_durations = [float(m) for m in re.findall(r"silence_duration:\s*([\d\.]+)", log_output)]

        # Если тишины вообще нет — файл живой
        if not silence_starts:
            return False

        total_silence = sum(silence_durations)

        # Случай А: Сплошной шум/тишина с самого начала и до конца (конец тишины не зафиксирован)
        if len(silence_starts) == 1 and silence_starts[0] == 0.0 and not silence_ends:
            logger.debug("Звонок полностью пустой (тишина с 0-й секунды).")
            return True

        # Случай Б: Тишина занимает более 92% всей записи (гудки, автоответчик)
        silence_ratio = total_silence / total_duration
        if silence_ratio > 0.92:
            logger.debug(f"Звонок признан пустым: {silence_ratio*100:.1f}% трека занимает тишина ({total_silence:.1f} сек из {total_duration:.1f} сек).")
            return True

        return False

    except FileNotFoundError:
        logger.error("Системная утилита 'ffmpeg' не найдена в вашей OpenSUSE. Выполните: sudo zypper install ffmpeg")
        return False
    except Exception as e:
        logger.error(f"Ошибка при вызове ffmpeg: {e}", exc_info=True)
        return False