import os
import subprocess
import tempfile
import traceback
import pty
from pydub import AudioSegment
from utils.logs import setup_logger

logger = setup_logger("transcriber")

# === НАСТРОЙКИ ПУТЕЙ К WHISPER.CPP ===
WHISPER_DIR = os.path.expanduser("~/Projects/analyze_novofon_new/whisper/whisper.cpp")
WHISPER_BIN = os.path.join(WHISPER_DIR, "build", "bin", "whisper-cli")
WHISPER_MODEL = os.path.join(WHISPER_DIR, "models", "ggml-large-v3-turbo.bin")


def transcribe_audio_locally(audio_bytes: bytes) -> str | None:
    print("\n[WHISPER.CPP DEBUG] >>> Вход в функцию транскрибации...", flush=True)
    
    if not os.path.exists(WHISPER_BIN):
        print(f"[WHISPER.CPP ❌] Бинарник не найден: {WHISPER_BIN}", flush=True)
        return None

    if not os.path.exists(WHISPER_MODEL):
        print(f"[WHISPER.CPP ❌] Модель не найдена: {WHISPER_MODEL}", flush=True)
        return None

    temp_mp3 = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
    temp_wav = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
    
    process = None
    try:
        print(f"[WHISPER.CPP DEBUG] 1. Записываем {len(audio_bytes)} байт во временный MP3...", flush=True)
        temp_mp3.write(audio_bytes)
        temp_mp3.close()

        print("[WHISPER.CPP DEBUG] 2. Конвертируем MP3 → WAV через pydub...", flush=True)
        try:
            audio = AudioSegment.from_file(temp_mp3.name, format="mp3")
            audio = audio.set_frame_rate(16000).set_channels(1).set_sample_width(2)
            audio.export(temp_wav.name, format="wav")
            temp_wav.close()
            print(f"[WHISPER.CPP DEBUG] 3. WAV успешно создан. Размер: {os.path.getsize(temp_wav.name)} байт", flush=True)
        except Exception as pydub_err:
            print(f"[WHISPER.CPP ❌] Ошибка pydub/ffmpeg: {pydub_err}", flush=True)
            return None

        cmd = [
            WHISPER_BIN,
            "-m", WHISPER_MODEL,
            "-f", temp_wav.name,
            "-l", "ru",
            "-nt"
        ]

        print(f"[WHISPER.CPP DEBUG] 4. Запуск PTY команды: {' '.join(cmd)}", flush=True)

        master_fd, slave_fd = pty.openpty()

        process = subprocess.Popen(
            cmd,
            stdout=slave_fd,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            close_fds=True
        )

        os.close(slave_fd)
        print("[WHISPER.CPP DEBUG] 5. Процесс запущен. Ожидаем поток вывода...", flush=True)

        transcript_lines = []
        
        # Используем стандартный os.fdopen
        with os.fdopen(master_fd, 'r', encoding='utf-8', errors='ignore') as pipe:
            for line in pipe:
                # Очищаем от мусора и спецсимволов прогресс-бара
                clean_line = line.replace('\r', '\n').strip()
                if clean_line:
                    for sub_line in clean_line.split('\n'):
                        sub_line = sub_line.strip()
                        if sub_line:
                            print(f"[WHISPER.CPP GPU OUTPUT]: {sub_line}", flush=True)
                            if not ("progress" in sub_line.lower() or "%" in sub_line):
                                transcript_lines.append(sub_line)

        print("[WHISPER.CPP DEBUG] 6. Поток вывода закрылся, ждем завершения процесса...", flush=True)
        process.wait(timeout=60)  # Таймаут на завершение после закрытия потока

        if process.returncode != 0:
            print(f"[WHISPER.CPP ❌] Бинарник завершился с ошибкой. Код: {process.returncode}", flush=True)
            return None

        transcript_text = "\n".join(transcript_lines)
        print(f"[WHISPER.CPP ✅] Готово! Получено {len(transcript_lines)} строк.", flush=True)
        return transcript_text

    except subprocess.TimeoutExpired:
        print("[WHISPER.CPP ❌] Таймаут выполнения процесса!", flush=True)
        if process:
            process.kill()
        return None
    except Exception as e:
        print(f"[WHISPER.CPP КРАХ]: {e}", flush=True)
        print(traceback.format_exc(), flush=True)
        return None
    finally:
        # Чистим за собой временные файлы
        for f in [temp_mp3.name, temp_wav.name]:
            try:
                if os.path.exists(f):
                    os.unlink(f)
            except Exception:
                pass