import subprocess
import re

from utils.logs import setup_logger

logger = setup_logger("watchdog")

def is_audio_empty(audio_bytes: bytes, noise_threshold_db: int = -40, min_silence_duration: float = 2.0) -> bool:
    """
    Проверяет, есть ли в аудиозаписи реальный разговор.
    Возвращает True, если звонок состоит только из тишины/гудков.
    """
    if not audio_bytes:
        return True

    # Запускаем ffmpeg, который принимает аудио из PIPE и анализирует его фильтром silencedetect
    cmd = [
        'ffmpeg', '-i', 'pipe:0',
        '-af', f'silencedetect=n={noise_threshold_db}dB:d={min_silence_duration}',
        '-f', 'null', '-'
    ]
    
    try:
        process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        _, stderr = process.communicate(input=audio_bytes, timeout=15)
        output = stderr.decode('utf-8', errors='ignore')
        
        # Вытаскиваем общую длительность трека
        duration_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", output)
        if not duration_match:
            return False # Если не смогли определить длительность, лучше перестраховаться и отправить в нейросеть
            
        hours, minutes, seconds = map(float, duration_match.groups())
        total_duration = hours * 3600 + minutes * 60 + seconds
        
        # Считаем суммарную длительность всех участков тишины
        silence_durations = re.findall(r"silence_duration:\s*(\d+\.\d+)", output)
        total_silence = sum(map(float, silence_durations))
        
        # Если тишина занимает более 90% всего времени звонка — разговора не было
        if total_duration > 0 and (total_silence / total_duration) > 0.90:
            return True
            
        return False
    except Exception as e:
        logger.error(f"Ошибка при анализе тишины через FFmpeg: {e}")
        return False # В случае бага не дропаем звонок, пусть идет в API