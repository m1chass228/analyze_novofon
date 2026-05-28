from __future__ import annotations

import requests
import time
import json
import base64
import ssl

from datetime import datetime, timedelta

from utils.logs import setup_logger
from tenacity import Retrying, stop_after_attempt, wait_exponential_jitter, retry_if_exception_type
import re

# Импортируем pydub для исправления бага с "глухотой" Gemini на первых секундах звонка
from pydub import AudioSegment
from pydub.generators import Sine

from requests.exceptions import RequestException, SSLError

from src.database import get_available_gas_url, block_gas_url

import config as cfg

# Отключаем строгую проверку конца TLS-протокола для Python 3.13 + OpenSSL 3.x
try:
    ctx = ssl.create_default_context()
    if hasattr(ssl, "OP_IGNORE_UNEXPECTED_EOF"):
        ctx.options |= ssl.OP_IGNORE_UNEXPECTED_EOF
except AttributeError:
    pass

UNKNOWN_VALUE = "Не определен"

logger = setup_logger("analyzer")

# === КАСТОМНЫЕ ИСКЛЮЧЕНИЯ ДЛЯ УПРАВЛЕНИЯ ЦИКЛАМИ RETRY ===
class ServiceUnavailableError(requests.RequestException):
    """Исключение для HTTP 503 — сервер временно перегружен, ТРЕБУЕТСЯ РЕТРАЙ."""
    pass

class QuotaExceededException(Exception):
    """Исключение для HTTP 429 или исчерпания лимитов квот — РЕТРАЙ НЕ НУЖЕН, меняем аккаунт."""
    pass


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
            logger.debug(f"| [DOWNLOAD] Попытка скачать запись для звонка {call_id}...")
            
            # Заворачиваем скачивание конкретного URL в tenacity
            for attempt in Retrying(
                stop=stop_after_attempt(3),
                wait=wait_exponential_jitter(initial=1, max=5, jitter=1),
                retry=retry_if_exception_type((RequestException, SSLError)),
                reraise=True
            ):
                with attempt:
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
            logger.debug(f"| [DOWNLOAD] Исключение (после ретраев) на {url}: {e}")

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
                "id", "start_time", "finish_time", "direction", "source", "is_lost", 
                "communication_id", "communication_type", "talk_duration", "wait_duration", 
                "total_duration", "clean_talk_duration", "contact_phone_number", 
                "virtual_phone_number", "finish_reason", "cpn_region_id", "cpn_region_name",
                "call_records", "wav_call_records", "last_answered_employee_id",
                "last_answered_employee_full_name", "first_answered_employee_id",
                "first_answered_employee_full_name", "employees"
            ]
        }
    }

    try:
        logger.info(f"[API] Запрос звонков: {hours_back}ч назад | {date_from} — {date_till}")
        
        # Заворачиваем запрос списка звонков в ретраи
        for attempt in Retrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential_jitter(initial=2, max=7, jitter=1),
            retry=retry_if_exception_type((RequestException, SSLError)),
            reraise=True
        ):
            with attempt:
                if attempt.retry_state.attempt_number > 1:
                    logger.warning(f"⏳ [API RETRY] Повторный запрос списка звонков, попытка №{attempt.retry_state.attempt_number}")
                resp = requests.post(cfg.NOVOFON_DATAAPI, json=payload, timeout=30)
                resp.raise_for_status()
        
        full_response = resp.json()
        print(full_response)
        result = full_response.get("result", {})
        data = result.get("data", [])
        
        logger.info(f"[API] Получено {len(data)} сессий звонков из API")

        calls = []
        skipped_no_record = 0

        if data and isinstance(data, list) and isinstance(data[0], dict):
            logger.info(f"[DEBUG_RAW_CALL] Сырая структура звонка: {json.dumps(data[0], ensure_ascii=False)}")

        for call in data:
            if not isinstance(call, dict):
                logger.warning(f"[API WARN] Элемент call пришел не в виде dict: {type(call)} -> {call}")
                continue

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
                    if isinstance(emp, dict) and emp.get("employee_full_name"):
                        admin_name = emp.get("employee_full_name").strip()
                        break
            
            if admin_name != UNKNOWN_VALUE:
                admin_name = re.sub(r'\s*-\s*\d+.*$', '', admin_name).strip()

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
                "call_records": records_to_use
            }

            if not records_to_use:
                skipped_no_record += 1
                base_call["record_id"] = None
                calls.append(base_call)
                continue

            for rec_id in records_to_use:
                record_call = base_call.copy()
                if isinstance(rec_id, dict):
                    record_call["record_id"] = str(rec_id.get("id", ""))
                else:
                    record_call["record_id"] = str(rec_id)
                calls.append(record_call)

        print(calls)
        logger.info(f"[API] Итого сформировано {len(calls)} записей | Без записи: {skipped_no_record}")
        return calls

    except Exception as e:
        logger.error(f"[API] Failed to fetch calls после всех попыток ретрая: {e}", exc_info=True)
        return []


def analyze_audio(audio_bytes: bytes, call_info: dict) -> str | None:
    """
    ГЛАВНЫЙ ДИСПЕТЧЕР: Вызывается из call.py.
    """
    if not audio_bytes or len(audio_bytes) < 1024:
        logger.warning("| [ANALYZE] Пустые или поврежденные байты аудио, отмена запроса.")
        return None

    c_id = str(call_info.get('id', 'unknown'))
    talk_duration = int(call_info.get('duration', 0))
    direction = str(call_info.get('direction', 'in'))

    # 1. Попытка через бесплатный пул GAS (is_paid_pool=0)
    logger.info(f"| [DISPATCHER] Отправка АУДИО звонка {c_id} в пул бесплатных GAS...")
    free_result = _execute_analysis_via_pool(
        c_id=c_id, 
        talk_duration=talk_duration, 
        audio_bytes=audio_bytes, 
        call_info=call_info, 
        direction=direction, 
        is_paid_pool=0
    )
    if free_result:
        return free_result

    # 2. Если бесплатный пул исчерпан — уходим на платный fallback
    logger.warning(f"🚨 [DISPATCHER] Бесплатный пул исчерпан. Переключаемся на платный fallback по аудио...")
    return analyze_audio_via_paid_api(
        c_id=c_id, 
        talk_duration=talk_duration, 
        audio_bytes=audio_bytes, 
        call_info=call_info, 
        direction=direction
    )


def _execute_analysis_via_pool(c_id: str, talk_duration: int, audio_bytes: bytes, call_info: dict, direction: str, is_paid_pool: int = 0) -> str | None:
    """Обобщенная подфункция перебора URL из выбранного пула с контролируемыми ретраями через tenacity"""
    audio_b64 = base64.b64encode(audio_bytes).decode('utf-8')
    
    payload = {
        'call_id': str(c_id),
        'phone': str(call_info.get('phone', '')),
        'duration': str(talk_duration),
        'direction': str(direction),
        'audio_base64': audio_b64,
        'mime_type': 'audio/mpeg',
        'generationConfig': {
            'temperature': 0.0,
            'responseMimeType': "application/json"
        }
    }

    tried_urls = set()
    pool_name = "PAID_GAS" if is_paid_pool == 1 else "FREE_GAS"

    def before_sleep_log(retry_state):
        logger.warning(f"⏳ [{pool_name} TENACITY] Попытка {retry_state.attempt_number} не удалась. Локальный перезапуск текущего URL...")

    while True:
        try:
            gas_url = get_available_gas_url(is_paid=is_paid_pool) if is_paid_pool == 1 else get_available_gas_url()
        except TypeError:
            gas_url = get_available_gas_url() if is_paid_pool == 0 else None
        
        if not gas_url or gas_url in tried_urls:
            logger.warning(f"⚠️ [{pool_name}] Доступные аккаунты в пуле закончились или все заблокированы.")
            break 

        try:
            logger.info(f"| [{pool_name}] Запрос к GAS: ...{gas_url[-25:]}")

            # Ретрай-политика tenacity охраняет от сетевых падений, HTTP 503 и "замаскированных" ошибок внутри JSON
            for attempt in Retrying(
                stop=stop_after_attempt(3),
                wait=wait_exponential_jitter(initial=3, max=15, jitter=2),
                retry=retry_if_exception_type((requests.RequestException, ServiceUnavailableError)),
                before_sleep=before_sleep_log,
                reraise=True
            ):
                with attempt:
                    response = requests.post(gas_url, json=payload, timeout=240)

                    if response.status_code == 503:
                        logger.warning(f"⏳ [{pool_name} 503] Сервер Google перегружен (High Demand). Запускаем tenacity retry...")
                        raise ServiceUnavailableError("Google 503 Service Unavailable")

                    result_text = response.text.strip()

                    # Обработка лимитов 429 (в HTTP-статусе или в тексте ответа Google Scripts)
                    if response.status_code == 429 or any(err in result_text for err in ["Gemini API error 429", "Too Many Requests", "Quota exceeded"]):
                        logger.warning(f"⚠️ [{pool_name} 429] На аккаунте кончились лимиты. Блок на 10 мин. Меняем прокси.")
                        block_gas_url(gas_url, duration_seconds=600)
                        raise QuotaExceededException()

                    if "Gemini API error 503" in result_text or "service is currently unavailable" in result_text:
                        logger.warning(f"⏳ [{pool_name} 503 Text] Сервер Google перегружен внутри ответа GAS. Ретраим через tenacity...")
                        raise ServiceUnavailableError("Google 503 Service Unavailable inside JSON")

                    if response.status_code != 200:
                        logger.error(f"| [{pool_name} HTTP {response.status_code}] Ошибка прокси. Блок на 45 сек.")
                        block_gas_url(gas_url, duration_seconds=45)
                        raise QuotaExceededException()

                    # Валидируем JSON перед возвратом и проверяем внутреннее поле "error"
                    try:
                        parsed_res = json.loads(result_text)
                        
                        # КРИТИЧЕСКИЙ ФИКС: Проверяем, не подсунул ли GAS ошибку под видом успешного JSON
                        if isinstance(parsed_res, dict) and "error" in parsed_res:
                            err_msg = str(parsed_res["error"])
                            if "503" in err_msg or "UNAVAILABLE" in err_msg:
                                logger.warning(f"⏳ [{pool_name} Fake 200] Обнаружена замаскированная ошибка 503. Ретраим...")
                                raise ServiceUnavailableError(f"Zamaskirovannaya 503: {err_msg}")
                            else:
                                logger.warning(f"| [{pool_name} JSON_ERROR_FIELD] В ответе поле error: {err_msg}. Блок на 1 мин.")
                                block_gas_url(gas_url, duration_seconds=60)
                                raise QuotaExceededException()
                        
                        print(result_text)
                        logger.info(f"✅ [{pool_name}] Успешно обработано через GAS для {c_id}")
                        return result_text
                    except json.JSONDecodeError:
                        logger.warning(f"| [{pool_name} JSON_ERR] Некорректный JSON от GAS. Блок на 1 мин.")
                        block_gas_url(gas_url, duration_seconds=60)
                        raise QuotaExceededException()

        except QuotaExceededException:
            # Управляемо переходим к следующему URL в пуле
            tried_urls.add(gas_url)
            continue
        except Exception as e:
            # Сюда залетаем, если ВСЕ попытки tenacity на данном URL (включая фейковые 503) провалились
            logger.warning(f"| [{pool_name} URL_FAILED] URL окончательно отвалился по таймауту/ошибкам: {e}")
            block_gas_url(gas_url, duration_seconds=45)
            tried_urls.add(gas_url)
            continue

    return None


def analyze_audio_via_paid_api(c_id: str, talk_duration: int, audio_bytes: bytes, call_info: dict, direction: str) -> str | None:
    """Резервный метод отправки напрямую на OpenRouter."""
    logger.info(f"🪙 [PAID_FALLBACK] Проверяем платный пул в базе данных...")
    paid_gas_result = _execute_analysis_via_pool(c_id, talk_duration, audio_bytes, call_info, direction, is_paid_pool=1)
    if paid_gas_result:
        return paid_gas_result

    logger.info(f"🪙 [PAID_FALLBACK] Платного пула в БД нет. Бьем напрямую в OPENROUTER_GAS_URL...")
    gas_url = getattr(cfg, 'OPENROUTER_GAS_URL', None)
    if not gas_url:
        logger.error("❌ КРИТИЧЕСКИ: OPENROUTER_GAS_URL отсутствует в config.py! Платный анализ невозможен.")
        return None

    dir_text = "ИСХОДЯЩИЙ звонок от администратора клиенту" if direction == "out" else "ВХОДЯЩИЙ звонок от клиента в клинику"
    
    full_prompt = f"""
Ты — профессиональный аудитор контроля качества звонков в ветеринарной клинике.
Прослушай запись и проведи строгий аудит по критериям качества.
ТЕКУЩИЙ КОНТЕКСТ ЗВОНКА: Это {dir_text}. Учти это при анализе!

### ИНСТРУКЦИИ ПО ИЗВЛЕЧЕНИЮ ФАКТОВ (КРИТИЧЕСКИ ВАЖНО):
1. ПРИВЕТСТВИЕ: Первые 3 секунды — технический паддинг (шум). Приветствие администратора начинается ПОСЛЕ 3-й секунды. Запиши точную дословную фразу в "raw_call_greeting".
3. ИМЯ И ФАМИЛИЯ АДМИНИСТРАТОРА: Вытащи реальное имя. Будь внимателен (Алевтина ≠ Альбина). Склей имя и фамилию за весь звонок. Если фамилии не было — только имя. Если имя не прозвучало — "Не представился(-ась)".
4. ИМЯ КЛИЕНТА И ПИТОМЦА: Зафиксируй имя владельца и кличку животного.

### СТРУКТУРА ОЦЕНКИ (максимум 43 балла):
1. Установление контакта (max 2)
2. Использование имени (max 2) — минимум 2 обращения по имени.
3. Выяснение потребностей (max 9)
4. Презентация услуг (max 7)
5. Презентация цен (max 3)
6. Работа с возражениями (max 4)
7. Ведение к записи (max 8)
8. Завершение диалога (max 3)
9. Индивидуальный подход (max 5)

### КАТЕГОРИИ:
1 (43-37), 2 (36-30), 3 (29-23), 4 (22 и менее).

### ВЫДАЙ СТРОГО В ФОРМАТЕ JSON (без markdown, без лишнего текста):
{{
  "raw_call_greeting": "Точный дословный текст приветствия",
  "admin_name": "Имя Фамилия администратора",
  "appointment_made": true/false,
  "total_score": число,
  "category": число,
  "dialog_overview": "Подробный пересказ ключевых моментов",
  "details": {{
    "contact": {{"score": число, "comment": "Обоснование"}},
    "name_usage": {{"score": число, "comment": "Обоснование"}},
    "needs_analysis": {{"score": число, "comment": "Обоснование"}},
    "presentation": {{"score": число, "comment": "Обоснование"}},
    "pricing": {{"score": число, "comment": "Обоснование"}},
    "objections": {{"score": число, "comment": "Обоснование"}},
    "closing_to_appointment": {{"score": число, "comment": "Обоснование"}},
    "termination": {{"score": число, "comment": "Обоснование"}},
    "individual_approach": {{"score": число, "comment": "Обоснование"}}
  }}
}}
"""
    try:
        audio_b64 = base64.b64encode(audio_bytes).decode('utf-8')
        payload = {
            'call_id': str(c_id),
            'phone': str(call_info.get('phone', '')),
            'duration': str(talk_duration),
            'direction': str(direction),
            'audio_base64': audio_b64,
            'mime_type': 'audio/mpeg',
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": full_prompt},
                        {"type": "input_audio", "input_audio": {"data": audio_b64, "format": "mp3"}}
                    ]
                }
            ],
            "model": "google/gemini-2.5-flash",
            "temperature": 0.0,
            "response_format": {"type": "json_object"}
        }

        def before_sleep_openrouter_log(retry_state):
            logger.warning(f"⏳ [OPENROUTER TENACITY] Попытка {retry_state.attempt_number} не удалась. Повторяем...")

        try:
            for attempt in Retrying(
                stop=stop_after_attempt(4),
                wait=wait_exponential_jitter(initial=5, max=30, jitter=3),
                retry=retry_if_exception_type((requests.RequestException, ServiceUnavailableError)),
                before_sleep=before_sleep_openrouter_log,
                reraise=True
            ):
                with attempt:
                    logger.info(f"🚀 [DIRECT_OPENROUTER] Запрос к API OpenRouter. Попытка №{attempt.retry_state.attempt_number or 1}")
                    response = requests.post(gas_url, json=payload, timeout=180)
                    
                    if response.status_code == 503:
                        logger.warning(f"⚠️ [DIRECT_OPENROUTER] Код 503 (Unavailable) от OpenRouter. Ретраим через tenacity...")
                        raise ServiceUnavailableError("OpenRouter 503 Service Unavailable")
                    
                    if response.status_code == 429:
                        logger.error("❌ [DIRECT_OPENROUTER] Ошибка 429 Лимиты исчерпаны. Немедленный выход.")
                        return None

                    if response.status_code != 200:
                        logger.error(f"❌ [DIRECT_OPENROUTER ERROR {response.status_code}] {response.text[:200]}")
                        return None

                    # Разбор ответа OpenRouter
                    try:
                        resp_json = response.json()
                        choices = resp_json.get("choices", [])
                        if choices:
                            content_text = choices[0].get("message", {}).get("content", "").strip()
                        else:
                            content_text = response.text.strip()
                    except Exception:
                        content_text = response.text.strip()

                    # Финальная очистка и валидация
                    try:
                        json.loads(content_text)
                        print(content_text)
                        return content_text
                    except Exception:
                        cleaned = content_text.replace("```json", "").replace("```", "").strip()
                        print(cleaned)
                        return cleaned

        except Exception as retry_exc:
            logger.error(f"❌ [OPENROUTER FAILED] Все попытки исчерпаны. Не удалось получить ответ: {retry_exc}")
            return None

    except Exception as e:
        logger.error(f"❌ Крах прямого OpenRouter запроса: {e}")
        return None