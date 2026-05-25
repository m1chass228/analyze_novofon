from __future__ import annotations

import requests
import time
import json
import io
import base64

from datetime import datetime, timedelta

from utils.logs import setup_logger
from tenacity import retry, stop_after_attempt, wait_exponential_jitter, retry_if_exception_type

# Импортируем pydub для исправления бага с "глухотой" Gemini на первых секундах звонка
from pydub import AudioSegment
from pydub.generators import Sine

import config as cfg

UNKNOWN_VALUE = "Не определен"

logger = setup_logger("analyzer")

# === МОД РАБОТЫ: GEMINI (по умолчанию) или WHISPER (локальная) ===
def get_analysis_mode():
    """Возвращает текущий режим анализа audio из config"""
    return getattr(cfg, 'AUDIO_ANALYSIS_MODE', 'gemini').lower().strip()

def get_record(call_id: str, record_id: str, communication_id: str = None) -> bytes | None:
    if not communication_id:
        communication_id = call_id

    urls_to_try = [
        f"https://app.novofon.ru/system/media/talk/{communication_id}/{record_id}/",
        f"https://my.novofon.ru/system/media/talk/{communication_id}/{record_id}/",
    ]

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://my.novofon.ru/",
        "Accept": "audio/mpeg, audio/wav, */*",
        "Origin": "https://my.novofon.ru"
    }

    for url in urls_to_try:
        try:
            r = requests.get(url, headers=headers, timeout=40)
            size_kb = len(r.content) // 1024
            content_type = r.headers.get('Content-Type', '').lower()

            if r.status_code == 200 and size_kb > 15:
                if "text/html" in content_type or "application/json" in content_type:
                    logger.debug(f"| [DOWNLOAD] ⚠ Fake success: got {content_type} instead of audio from {url}")
                    continue
                
                logger.info(f"| [DOWNLOAD] ✅ Success: {call_id} ({size_kb} KB, Type: {content_type}) | {url}")
                return r.content
            else:
                logger.debug(f"| [DOWNLOAD] Attempt failed: {url} → {r.status_code} ({size_kb} KB)")
        except Exception as e:
            logger.debug(f"| [DOWNLOAD] Exception on {url}: {e}")

    logger.warning(f"| [DOWNLOAD] ❌ All attempts failed for {call_id}")
    return None


def get_calls(hours_back: int = 24):
    """Получает список звонков с записями"""
    now = datetime.now()
    date_from = (now - timedelta(hours=hours_back)).strftime("%Y-%m-%d %H:%M:%S")
    date_till = (now + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")

    payload = {
        "jsonrpc": "2.0",
        "method": "get.calls_report",
        "id": int(time.time()),
        "params": {
            "access_token": cfg.NOVOFON_TOKEN,
            "date_from": date_from,
            "date_till": date_till,
            "limit": 500,
            "offset": 0,
            "include_ongoing_calls": False,
            "fields": [
                "id", 
                "start_time", 
                "finish_time", 
                "direction", 
                "source", 
                "is_lost", 
                "communication_id", 
                "communication_type",
                "talk_duration", 
                "wait_duration", 
                "total_duration", 
                "clean_talk_duration",
                "contact_phone_number", 
                "virtual_phone_number", 
                "finish_reason", 
                "cpn_region_id", 
                "cpn_region_name",
                "call_records", 
                "wav_call_records",
                "last_answered_employee_id",
                "last_answered_employee_full_name",
                "first_answered_employee_id",
                "first_answered_employee_full_name",
                "employees"
            ]
        }
    }

    try:
        logger.info(f"[API] Запрос звонков: {hours_back}ч назад | {date_from} — {date_till}")
        
        resp = requests.post(cfg.NOVOFON_DATAAPI, json=payload, timeout=30)
        resp.raise_for_status()
        
        full_response = resp.json()
        result = full_response.get("result", {})
        data = result.get("data", [])
        
        logger.info(f"[API] Получено {len(data)} сессий звонков из API")

        calls = []
        skipped_no_record = 0

        if data:
            logger.info(f"[DEBUG_RAW_CALL] Сырая структура звонка: {json.dumps(data[0], ensure_ascii=False)}")

        for call in data:
            call_id = str(call.get("id"))
            communication_id = str(call.get("communication_id") or call.get("id"))
            start_time = call.get("start_time")
            talk_duration = int(call.get("talk_duration") or call.get("duration") or 0)
            
            admin_name = UNKNOWN_VALUE

            if call.get("last_answered_employee_full_name"):
                admin_name = call.get("last_answered_employee_full_name").strip()
            elif call.get("first_answered_employee_full_name"):
                admin_name = call.get("first_answered_employee_full_name").strip()
            
            if admin_name == UNKNOWN_VALUE:
                for emp in (call.get("employees") or []):
                    if emp.get("employee_full_name"):
                        admin_name = emp.get("employee_full_name").strip()
                        break

            call_records = call.get("call_records") or []
            wav_records = call.get("wav_call_records") or []
            records_to_use = call_records or wav_records

            base_call = {
                "id": call_id,
                "communication_id": communication_id,
                "start_time": start_time,
                "duration": talk_duration,
                "phone": call.get("contact_phone_number"),
                "virtual_phone_number": call.get("virtual_phone_number"),
                "admin_name": admin_name,
                "is_lost": call.get("is_lost", False),
                "direction": call.get("direction"),
            }

            if not records_to_use:
                skipped_no_record += 1
                base_call["record_id"] = None
                calls.append(base_call)
                continue

            for rec_id in records_to_use:
                record_call = base_call.copy()
                record_call["record_id"] = str(rec_id)
                calls.append(record_call)

        logger.info(f"[API] Итого сформировано {len(calls)} записей | Без записи: {skipped_no_record}")

        if calls:
            logger.debug(f"Пример звонка: {json.dumps(calls[0], ensure_ascii=False, indent=2)[:600]}")

        print(calls)

        return calls

    except Exception as e:
        logger.error(f"[API] Failed to fetch calls: {e}", exc_info=True)
        return []
    
def analyze_audio(audio_bytes: bytes, call_info: dict) -> str | None:
    """
    Анализирует аудио: либо через Gemini Apps Script, либо через локальный Whisper + Gemini.
    Режим работы переключается в config.AUDIO_ANALYSIS_MODE ('gemini' | 'whisper').
    """
    mode = get_analysis_mode()

    if mode == 'whisper':
        logger.info(f"| [MODE] Whisper → Gemini pipeline активен для {call_info.get('id', 'unknown')}")
        return _analyze_audio_whisper_pipeline(audio_bytes, call_info)
    else:
        logger.info(f"| [MODE] Gemini direct (legacy) для {call_info.get('id', 'unknown')}")
        return _analyze_audio_gemini(audio_bytes, call_info)


@retry(
    retry=retry_if_exception_type((requests.exceptions.RequestException, Exception)),
    wait=wait_exponential_jitter(initial=12, max=120),
    stop=stop_after_attempt(15),
    reraise=True
)
@retry(
    retry=retry_if_exception_type((requests.exceptions.RequestException, Exception)),
    wait=wait_exponential_jitter(initial=15, max=180),
    stop=stop_after_attempt(5),
    reraise=True
)
def _analyze_audio_gemini(audio_bytes: bytes, call_info: dict) -> str | None:
    
    c_id = call_info.get('id', 'unknown')
    direction = call_info.get('direction', 'in') 
    size_kb = len(audio_bytes) / 1024
    logger.info(f"| [APPS_SCRIPT] Отправка записи {c_id} [{direction}], исходный размер: {size_kb:.1f} KB")

    try:
        # === ХАК: Подмешиваем искусственный сигнал на старте, чтобы разбудить VAD у Gemini Flash-Lite ===
        try:
            logger.debug(f"| [PYDUB] Накатываем пре-паддинг шума на звонок {c_id}")
            # Загружаем байты mp3 из Новофона в pydub
            original_audio = AudioSegment.from_file(io.BytesIO(audio_bytes), format="mp3")
            
            # Генерируем 3 секунды (3000 мс) очень тихого (-40 dB) звукового синуса (440 Гц)
            # Это заставит алгоритмы Google считать, что полезный звук идет с самого начала
            padding = Sine(440).to_audio_segment(duration=3000).apply_gain(-40)
            
            # Склеиваем пре-паддинг с оригинальной записью
            padded_audio = padding + original_audio
            
            # Выгружаем результат обратно в байты mp3
            output_buffer = io.BytesIO()
            padded_audio.export(output_buffer, format="mp3")
            processed_bytes = output_buffer.getvalue()
            
            logger.debug(f"| [PYDUB] Пре-паддинг успешно добавлен. Размер файла: {len(processed_bytes)/1024:.1f} KB")
        except Exception as pydub_err:
            # Если ffmpeg не установлен или pydub упал — логируем, но не ломаем скрипт, шлём оригинал
            logger.error(f"| [PYDUB] ⚠ Не удалось применить фикс аудио: {pydub_err}. Отправляем сырые байты.")
            processed_bytes = audio_bytes

        # Кодируем обработанные байты в Base64
        # Кодируем обработанные байты в Base64
        audio_b64 = base64.b64encode(processed_bytes).decode('utf-8')

        # Формируем системный промпт (инструкцию для анализа)
        system_instruction = (
            f"Ты — аудитор звонков в медицинском центре. Проанализируй этот звонок. "
            f"Информация о звонке: ID: {c_id}, Направление: {direction}, Длительность: {call_info.get('duration', '')} сек."
        )

        # Собираем OpenAI/OpenRouter-совместимую структуру
        payload = {
            "messages": [
                {
                    "role": "system",
                    "content": system_instruction
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Сделай транскрибацию и заполни JSON-отчет по правилам клиники."
                        },
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": audio_b64,
                                "format": "mp3"  # Для 'audio/mpeg' (mp3) передаем именно "mp3"
                            }
                        }
                    ]
                }
            ],
            "temperature": 0.0,
            "response_format": {
                "type": "json_object"
            }
        }

        response = requests.post(
            cfg.APPS_SCRIPT_URL,
            json=payload,
            timeout=360
        )

        if response.status_code == 200:
            result_text = response.text.strip()
            logger.info(f"| [APPS_SCRIPT] Response OK for {c_id}")

            if "Gemini API error 503" in result_text or "service is currently unavailable" in result_text:
                logger.warning(f"| [GEMINI 503] Сервис перегружен для {c_id}, будет retry")
                raise Exception(f"Gemini 503 Overload: {result_text[:300]}")

            try:
                json.loads(result_text)
                print(result_text)
                return result_text
            except:
                logger.warning(f"| [JSON] Некорректный JSON от GAS: {result_text[:300]}")
                return None
        else:
            logger.error(f"| [HTTP {response.status_code}] {response.text[:200]}")
            return None
    except Exception as e:
        logger.error(f"✗ Исключение при вызове Apps Script для {c_id}: {e}")
        if "Gemini 503" in str(e):
            raise
        return None
    
@retry(
    retry=retry_if_exception_type((requests.exceptions.RequestException, Exception)),
    wait=wait_exponential_jitter(initial=12, max=120),
    stop=stop_after_attempt(15),
    reraise=True
)
@retry(
    retry=retry_if_exception_type((requests.exceptions.RequestException, Exception)),
    wait=wait_exponential_jitter(initial=15, max=180),
    stop=stop_after_attempt(5),
    reraise=True
)
def _analyze_audio_whisper_pipeline(audio_bytes: bytes, call_info: dict) -> str | None:
    """
    НОВЫЙ РЕЖИМ: Whisper (локально) → текст → Gemini (Apps Script, TextIn).
    Транскрибирует аудио локально, затем шлёт транскрипт + метаданные в Gemini.
    """
    from transcriber import transcribe_audio_locally

    c_id = call_info.get('id', 'unknown')
    direction = call_info.get('direction', 'in')

    logger.info(f"| [WHISPER→GEMINI] Начинаем конвейер для {c_id} [{direction}]")

    # --- Шаг 1: Локальная транскрибация через Whisper ---
    transcript_text = transcribe_audio_locally(audio_bytes)

    if not transcript_text:
        logger.error(f"| [WHISPER→GEMINI] ⚠ Транскрипт пуст для {c_id}. Пропускаем.")
        return None

    word_count = len(transcript_text.split())
    logger.info(f"| [WHISPER→GEMINI] Транскрипт получен: {word_count} слов, {len(transcript_text)} символов")
    logger.debug(f"| [WHISPER→GEMINI] Первые 300 символов транскрипта: {transcript_text[:300]}")

    # --- Шаг 2: Отправляем транскрипт в Gemini Apps Script как текст ---
    try:
        payload = {
            'call_id': str(c_id),
            'phone': str(call_info.get('phone', '')),
            'duration': str(call_info.get('duration', '')),
            'direction': str(direction),
            'transcript': transcript_text,
            'generationConfig': {
                'temperature': 0.0,
                'responseMimeType': "application/json"
            }
        }

        response = requests.post(cfg.APPS_SCRIPT_URL, json=payload, timeout=360)

        if response.status_code == 200:
            result_text = response.text.strip()
            logger.info(f"| [WHISPER→GEMINI] Response OK for {c_id}")

            if "Gemini API error 503" in result_text or "service is currently unavailable" in result_text:
                logger.warning(f"| [GEMINI 503] Сервис перегружен для {c_id}, будет retry")
                raise Exception(f"Gemini 503 Overload: {result_text[:300]}")

            try:
                json.loads(result_text)
                print(result_text)
                return result_text
            except:
                logger.warning(f"| [JSON] Некорректный JSON от GAS: {result_text[:300]}")
                return None
        else:
            logger.error(f"| [HTTP {response.status_code}] {response.text[:200]}")
            return None
    except Exception as e:
        logger.error(f"| [WHISPER→GEMINI] Ошибка отправки в Apps Script для {c_id}: {e}")
        if "Gemini 503" in str(e):
            raise
        return None


