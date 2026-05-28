from __future__ import annotations

import time
import json
import base64
import ssl
import re
import httpx
import asyncio

from datetime import datetime, timedelta
from tenacity import AsyncRetrying, stop_after_attempt, wait_exponential_jitter, retry_if_exception_type

from utils.logs import setup_logger
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
class ServiceUnavailableError(Exception):
    """Исключение для HTTP 503 или Gemini API error 503.
    Сервер временно перегружен, ТРЕБУЕТСЯ РЕТРАЙ ТЕКУЩЕГО URL.
    """
    pass

class QuotaExceededException(Exception):
    """Исключение для HTTP 429 или исчерпания лимитов квот.
    РЕТРАЙ ТЕКУЩЕГО URL НЕ НУЖЕН, прерываем tenacity и в цикле while True меняем аккаунт/URL.
    """
    pass


def get_analysis_mode():
    """Возвращает текущий режим анализа audio из config"""
    return getattr(cfg, 'AUDIO_ANALYSIS_MODE', 'gemini').lower().strip()


async def get_record(call_id: str, record_id: str, communication_id: str = None) -> bytes | None:
    if not communication_id:
        communication_id = call_id

    url = f"{cfg.NOVOFON_BASE}system/media/talk/{communication_id}/{record_id}/"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://my.novofon.ru/",
        "Accept": "audio/mpeg, audio/wav, */*",
        "Origin": "https://my.novofon.ru"
    }

    async with httpx.AsyncClient() as  client: 
        try:
            logger.debug(f"| [DOWNLOAD] Попытка скачать запись для звонка {call_id}...")
            
            # Заворачиваем скачивание конкретного URL в tenacity
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(6),
                wait=wait_exponential_jitter(initial=1, max=5, jitter=1),
                retry=retry_if_exception_type(httpx.HTTPError),
                reraise=True
            ):
                with attempt:
                    response = await client.get(url, headers=headers, timeout=40)
                    response.raise_for_status()
            
            size_kb = len(response.content) // 1024
            content_type = response.headers.get('Content-Type', '').lower()

            if response.status_code == 200 and size_kb > 15:
                if "text/html" in content_type or "application/json" in content_type:
                    logger.debug(f"| [DOWNLOAD] Fake success: got {content_type} instead of audio from {url}")
                    return None
                
                logger.info(f"| [DOWNLOAD] Success: {call_id} ({size_kb} KB, Type: {content_type}) | {url}")
                return response.content
            else:
                logger.debug(f"| [DOWNLOAD] Attempt failed: {url} → {response.status_code} ({size_kb} KB)")
                
        except Exception as e:
            logger.debug(f"| [DOWNLOAD] Exception (after retries) on {url}: {e}")

    logger.warning(f"| [DOWNLOAD] All attempts failed for {call_id}")
    return None

# Хелпер для красивого вычленения имени админа
def _extract_admin_name(call: dict) -> str:
    admin_name = call.get("last_answered_employee_full_name") or call.get("first_answered_employee_full_name")
    
    # Если в основных полях пусто, ищем в списке employees
    if not admin_name:
        for emp in (call.get("employees") or []):
            if isinstance(emp, dict) and emp.get("employee_full_name"):
                admin_name = emp.get("employee_full_name")
                break
                
    if admin_name:
        # Очищаем имя от лишних хвостов через регулярку, убираем пробелы
        return re.sub(r'\s*-\s*\d+.*$', '', admin_name.strip()).strip()
    return UNKNOWN_VALUE

async def get_calls() -> list[dict]:
    """Получает список звонков с записями"""
    now = datetime.now()
    date_from = (now - timedelta(hours=cfg.CALLS_HOURS_BACK)).strftime("%Y-%m-%d %H:%M:%S")
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

    async with httpx.AsyncClient() as  client: 
        try:
            logger.info(f"[API] Requesting calls: {cfg.CALLS_HOURS_BACK}h back | {date_from} — {date_till}")
            
            # Заворачиваем запрос списка звонков в ретраи
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(6),
                wait=wait_exponential_jitter(initial=1, max=5, jitter=1),
                retry=retry_if_exception_type(httpx.HTTPError),
                reraise=True
            ):
                with attempt:
                    print(f"requesting novofon, {attempt.retry_state.attempt_number} try")
                    response = await client.post(cfg.NOVOFON_DATAAPI, json=payload, timeout=40)
                    response.raise_for_status()
            
            full_response = response.json()

            # === ПРОВЕРКА НА ОШИБКИ JSON-RPC ===
            if "error" in full_response:
                error_info = full_response["error"]
                error_code = error_info.get("code")
                error_msg = error_info.get("message", "Unknown RPC Error")
                
                logger.error(f"[API ERROR] Error JSON-RPC from server. (Error code: {error_code}): {error_msg}")
                if "token" in error_msg.lower() or error_code in (-32000, 401):
                    logger.critical("[CRITICAL ERROR]: Token from novofon is out of date, or not valid. All responces are canceled.")
                return []

            data = full_response.get("result", {}).get("data", [])
            logger.info(f"[API] Capture {len(data)} calls from API")

            if data and isinstance(data, list) and isinstance(data[0], dict):
                logger.debug(f"[DEBUG_RAW_CALL] Raw call structure: {json.dumps(data[0], ensure_ascii=False)}")

            calls = []
            skipped_no_record = 0

            for call in data:
                if not isinstance(call, dict):
                    continue

                records_to_use = call.get("call_records") or call.get("wav_call_records") or []

                base_call = {
                    "id": str(call.get("id")),
                    "communication_id": str(call.get("communication_id") or call.get("id")),
                    "start_time": call.get("start_time"),
                    "duration": int(call.get("talk_duration") or call.get("duration") or 0),
                    "phone": call.get("contact_phone_number"),
                    "virtual_phone_number": call.get("virtual_phone_number"),
                    "admin_name": _extract_admin_name(call),
                    "is_lost": call.get("is_lost", False),
                    "direction": call.get("direction"),
                    "call_records": records_to_use
                }

                if not records_to_use:
                    skipped_no_record += 1
                    base_call["record_id"] = None
                    calls.append(base_call)
                    continue

                # Раскладываем сессию звонка на отдельные записи, если их несколько
                for rec in records_to_use:
                    record_call = base_call.copy()
                    record_call["record_id"] = str(rec.get("id", "")) if isinstance(rec, dict) else str(rec)
                    calls.append(record_call)

            logger.info(f"[API] Total formed {len(calls)} records for processing | Без записи: {skipped_no_record}")
            return calls

        except Exception as e:
            logger.error(f"[API] Failed to fetch calls after all retires: {e}", exc_info=True)
            return []


async def analyze_audio(audio_bytes: bytes, call_info: dict) -> str | None:
    """ГЛАВНЫЙ АСИНХРОННЫЙ ДИСПЕТЧЕР"""
    if not audio_bytes or len(audio_bytes) < 1024:
        logger.warning("| [ANALYZE] Empty or corrupted audio bytes, request canceled.")
        return None

    c_id = str(call_info.get('id', 'unknown'))
    talk_duration = int(call_info.get('duration', 0))
    direction = str(call_info.get('direction', 'in'))

    # 1. Попытка через бесплатный пул GAS (is_paid_pool=0)
    logger.info(f"| [DISPATCHER] Sending AUDIO call {c_id} in the pool with free GAS...")
    free_result = await _execute_analysis_via_pool(
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
    return await analyze_audio_via_paid_api(
        c_id=c_id,  
        talk_duration=talk_duration,  
        audio_bytes=audio_bytes,  
        call_info=call_info,  
        direction=direction
    )


async def _execute_analysis_via_pool(c_id: str, talk_duration: int, audio_bytes: bytes, call_info: dict, direction: str, is_paid_pool: int = 0) -> str | None:
    """Асинхронная подфункция перебора URL из пула"""
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
        logger.warning(f"[{pool_name} TENACITY] Attempt {retry_state.attempt_number} failed. Local restart of current URL...")

    async with httpx.AsyncClient() as client:
        while True:
            try:
                gas_url = get_available_gas_url(is_paid=is_paid_pool) if is_paid_pool == 1 else get_available_gas_url()
            except TypeError:
                gas_url = get_available_gas_url() if is_paid_pool == 0 else None
            
            if not gas_url or gas_url in tried_urls:
                logger.warning(f"[{pool_name}] Available accounts in pool are run out.")
                break 

            try:
                logger.info(f"| [{pool_name}] Request to GAS: ...{gas_url[-25:]}")

                # Ретрай-политика tenacity охраняет от сетевых падений, HTTP 503 и "замаскированных" ошибок внутри JSON
                async for attempt in AsyncRetrying(
                    stop=stop_after_attempt(3),
                    wait=wait_exponential_jitter(initial=3, max=15, jitter=2),
                    retry=retry_if_exception_type((httpx.HTTPError, ServiceUnavailableError)),
                    before_sleep=before_sleep_log,
                    reraise=True
                ):
                    with attempt:
                        response = await client.post(gas_url, json=payload, timeout=240.0, follow_redirects=True)

                        if response.status_code == 503:
                            logger.warning(f"[{pool_name} 503] Server Google on High Demand. running tenacity retry...")
                            raise ServiceUnavailableError("Google 503 Service Unavailable")

                        result_text = response.text.strip()

                        # Обработка лимитов 429 (в HTTP-статусе или в тексте ответа Google Scripts)
                        if response.status_code == 429 or any(err in result_text for err in ["Gemini API error 429", "Too Many Requests", "Quota exceeded"]):
                            logger.warning(f"[{pool_name} 429] The account has reached its limits. Blocked for 10 minutes. Change the proxy.")
                            block_gas_url(gas_url, duration_seconds=600)
                            raise QuotaExceededException()

                        if "Gemini API error 503" in result_text or "service is currently unavailable" in result_text:
                            logger.warning(f"⏳ [{pool_name} 503 Text] Google server is overloaded within the response. Retrying via tenacity...")
                            raise ServiceUnavailableError("Google 503 Service Unavailable inside JSON")

                        if response.status_code == 302:
                            location = response.headers.get('Location', 'No Location header')
                            logger.error(f"| [{pool_name} HTTP 302 REDIRECT] → {location}")
                            logger.warning(f"Google требует авторизацию или прокси забанен. Баним URL.")
                            block_gas_url(gas_url, duration_seconds=300)  # 5 минут
                            raise QuotaExceededException()

                        elif response.status_code != 200:
                            logger.error(f"| [{pool_name} HTTP {response.status_code}] Proxy error.")
                            block_gas_url(gas_url, duration_seconds=45)
                            raise QuotaExceededException()

                        # Валидируем JSON перед возвратом и проверяем внутреннее поле "error"
                        try:
                            parsed_res = json.loads(result_text)
                            if isinstance(parsed_res, dict) and "error" in parsed_res:
                                err_msg = str(parsed_res["error"])
                                if "503" in err_msg or "UNAVAILABLE" in err_msg:
                                    logger.warning(f"⏳ [{pool_name} Fake 200] Found hidding error 503. Retrying...")
                                    raise ServiceUnavailableError(f"Hidden 503: {err_msg}")
                                else:
                                    logger.warning(f"| [{pool_name} JSON_ERROR_FIELD] В ответе поле error: {err_msg}. Блок на 1 мин.")
                                    block_gas_url(gas_url, duration_seconds=60)
                                    raise QuotaExceededException()
                            
                            print(result_text)
                            logger.info(f"✅ [{pool_name}] Successfully processed via GAS for {c_id}")
                            return result_text
                        except json.JSONDecodeError:
                            logger.warning(f"| [{pool_name} JSON_ERR] incorrect JSON from GAS. Block for 1 min.")
                            block_gas_url(gas_url, duration_seconds=60)
                            raise QuotaExceededException()

            except QuotaExceededException:
                tried_urls.add(gas_url)
                continue
            except Exception as e:
                logger.warning(f"| [{pool_name} URL_FAILED] URL Permanently dropped out due to timeouts/errors.: {e}")
                block_gas_url(gas_url, duration_seconds=45)
                tried_urls.add(gas_url)
                continue

    return None


async def analyze_audio_via_paid_api(c_id: str, talk_duration: int, audio_bytes: bytes, call_info: dict, direction: str) -> str | None:
    """Резервный метод отправки напрямую на OpenRouter."""
    logger.info(f"[PAID_FALLBACK] Checking the paid pool in the database....")
    paid_gas_result = await _execute_analysis_via_pool(c_id, talk_duration, audio_bytes, call_info, direction, is_paid_pool=1)
    if paid_gas_result:
        return paid_gas_result

    logger.info(f"[PAID_FALLBACK] There is no paid pool in the database. We hit directly in OPENROUTER_GAS_URL...")
    gas_url = getattr(cfg, 'OPENROUTER_GAS_URL', None)
    if not gas_url:
        logger.error("[CRITICAL ERROR]: OPENROUTER_GAS_URL is absent in config.py! Paid analysis is not possible..")
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
7. Ведение к записи (max 8)-
8. Завершение диалога (max 3)
9. Индивидуальный подход (max 5)

### КАТЕГОРИИ:
1 (43-37), 2 (36-30), 3 (29-23), 4 (22 и менее).
-
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
            logger.warning(f"[OPENROUTER TENACITY] Retry {retry_state.attempt_number} It didn't work out. Retrying...")

        try:
            async with httpx.AsyncClient() as client:
                async for attempt in AsyncRetrying(
                    stop=stop_after_attempt(4),
                    wait=wait_exponential_jitter(initial=5, max=30, jitter=3),
                    retry=retry_if_exception_type((httpx.HTTPError, ServiceUnavailableError)),
                    before_sleep=before_sleep_openrouter_log,
                    reraise=True
                ):
                    with attempt:
                        logger.info(f"[DIRECT_OPENROUTER] Request to API OpenRouter. Retry №{attempt.retry_state.attempt_number or 1}")
                        response = await client.post(gas_url, json=payload, timeout=180.0)
                        
                        if response.status_code == 503:
                            logger.warning(f"[DIRECT_OPENROUTER] Error code 503 (Unavailable) from OpenRouter. Retrying via tenacity...")
                            raise ServiceUnavailableError("OpenRouter 503 Service Unavailable")
                        
                        if response.status_code == 429:
                            logger.error("[DIRECT_OPENROUTER] Error 429: Limits exceeded. Immediate exit.")
                            return None

                        if response.status_code != 200:
                            logger.error(f"[DIRECT_OPENROUTER ERROR {response.status_code}] {response.text[:200]}")
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
            logger.error(f"[OPENROUTER FAILED] All attempts exhausted. Failed to receive a response.: {retry_exc}")
            return None

    except Exception as e:
        logger.error(f" Крах прямого OpenRouter запроса: {e}")
        return None