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

# Ручное сопоставление имени линии/сотрудника -> внутренний номер, для случаев,
# когда Новофон вообще не пишет цифры в employee_full_name (напр. "Татьяна Изосимова").
# Взято из присланной формы статистики. Ключи регистронезависимы.
# Можно дополнить/переопределить через cfg.EMPLOYEE_NAME_TO_NUMBER в config.py —
# особенно для "AdminKolomenskoe" и "Admin_trubka", у которых номер сейчас неизвестен.
DEFAULT_EMPLOYEE_NAME_TO_NUMBER = {
    "татьяна изосимова": "106",
    "раупова индира": "109",
    "индира раупова": "109",
    "admin_trubka": "100",       # Бирюлево, второй админ (по данным выгрузки Новофона от 06.08)
    "adminkolomenskoe": "130",   # Коломенское — уточнить у клиники, что это точно 130, а не 131
}

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
        f"https://app.novofon.ru/system/media/talk/{communication_id}/{record_id}/"
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
            
            raw_admin_name = None
            if call.get("last_answered_employee_full_name"):
                raw_admin_name = call.get("last_answered_employee_full_name").strip()
            elif call.get("first_answered_employee_full_name"):
                raw_admin_name = call.get("first_answered_employee_full_name").strip()

            employees_list = call.get("employees") or []
            if not raw_admin_name:
                for emp in employees_list:
                    if isinstance(emp, dict) and emp.get("employee_full_name"):
                        raw_admin_name = emp.get("employee_full_name").strip()
                        break

            # === Внутренний номер для ОТЧЁТОВ (столбец G) — пишем как прислал Новофон,
            # без парсинга: "103 Povodok", "114 Stacionar", "Татьяна Изосимова",
            # "AdminKolomenskoe" и т.д. Если линии вообще нет — "Не определен".
            internal_number = raw_admin_name.strip() if raw_admin_name else UNKNOWN_VALUE
            admin_name = internal_number

            # === Отдельно — чистое число для ГРУППИРОВКИ в файле статистики
            # (STATS_GROUPS сравнивает по числам типа "101", "120" и т.д.) ===
            stats_number = ""
            for key in ("last_answered_employee_extension_phone_number",
                        "first_answered_employee_extension_phone_number",
                        "extension_phone_number"):
                val = call.get(key)
                if val:
                    stats_number = re.sub(r'\D', '', str(val))
                    break

            if not stats_number:
                for emp in employees_list:
                    if isinstance(emp, dict) and emp.get("extension_phone_number"):
                        stats_number = re.sub(r'\D', '', str(emp.get("extension_phone_number")))
                        break

            if not stats_number and (raw_admin_name or employees_list):
                # Пробуем ПО ОЧЕРЕДИ имена всех сотрудников, участвовавших в звонке —
                # если первое имя не распознаётся, возможно распознается следующее
                # (напр. "AdminMaryino120, Индира Раупова" — второе имя уже есть в таблице)
                name_map = {**DEFAULT_EMPLOYEE_NAME_TO_NUMBER}
                for k, v in (getattr(cfg, 'EMPLOYEE_NAME_TO_NUMBER', {}) or {}).items():
                    name_map[str(k).strip().lower()] = str(v)

                candidate_names = [raw_admin_name] if raw_admin_name else []
                for emp in employees_list:
                    if isinstance(emp, dict) and emp.get("employee_full_name"):
                        candidate_names.append(emp.get("employee_full_name").strip())

                for name in candidate_names:
                    if not name:
                        continue
                    m = re.match(r'^(\d{2,4})(?=\D|$)', name) or re.search(r'(\d{2,4})\s*$', name)
                    if m:
                        stats_number = m.group(1)
                        break
                    name_key = re.sub(r'\s+', ' ', name).strip().lower()
                    if name_key in name_map:
                        stats_number = name_map[name_key]
                        break

            if not stats_number:
                logger.debug(
                    f"[INTERNAL_NUMBER] Не удалось определить номер для статистики звонка "
                    f"{call.get('id')}: raw_admin_name={raw_admin_name!r}, "
                    f"employees={employees_list}"
                )
            elif call is data[0]:
                logger.info(
                    f"[DEBUG_INTERNAL_NUMBER] Пример: raw_admin_name={raw_admin_name!r} "
                    f"-> internal_number(для отчёта)={internal_number!r}, stats_number(для статистики)={stats_number!r}"
                )

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
                "internal_number": internal_number,
                "stats_number": stats_number,
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
Ты — эксперт по контролю качества звонков ветеринарной клиники (Промт v2).
ТЕКУЩИЙ КОНТЕКСТ ЗВОНКА: Это {dir_text}. Учти это при анализе.

Ты — эксперт по контролю качества звонков ветеринарной клиники. Оцени диалог администратора с клиентом строго по методике ниже. Не занижай и не завышай баллы произвольно — каждая оценка должна быть обоснована конкретной репликой из диалога.

### ШАГ 1. Классификация звонка (обязательно, до оценки баллов)

Определи `call_type` — один из вариантов:
- `продающий` — клиент рассматривает услугу/запись, есть пространство для полноценной продажи
- `информационный` — узнать цену, часы работы, состав анализа и т.п., без намерения записаться в моменте
- `лаборатория_или_курьер` — служебный звонок между клиникой и лабораторией/сервисом
- `текущий_пациент` — уточнение по уже открытому случаю (стационар, назначения, результаты)
- `отказ` — клиент явно и обоснованно отказывается (цена, расстояние, не актуально и т.п.)

Классификация должна быть подтверждена цитатой в поле `call_type_evidence`. **Тип звонка влияет на то, какое поведение ожидается от администратора в каждой категории — но не освобождает категорию от оценки и не устанавливает автоматический максимум.** Вместо «не снижать» действует принцип «оценивай то поведение, которое было уместно и возможно в данном типе звонка» (см. рубрики ниже).

### ШАГ 1.5. Целевой / нецелевой звонок

Отдельно от `call_type` определи `call_target`:

- **`целевой`** — человеку нужна услуга клиники, и звонок в идеале должен привести к записи. Относится и к входящим (клиент сам звонит по поводу услуги), и к исходящим — например, когда оператор перезванивает клиенту по пропущенному звонку с целью записать его на приём.
- **`нецелевой`** — обращение не про запись как таковую: уточнить назначения, готов ли анализ, узнать результат, служебный звонок с лабораторией и т.п. Даже если разговор в итоге закончился хорошо, но изначальная цель клиента не была связана с записью — это нецелевой звонок.

Обязательно заполни поле `call_target_evidence` — цитата или краткое пояснение, почему звонок отнесён к тому или иному классу.

**В начале поля `dialog_overview` для целевых звонков добавь маркер «Целевой.»** (первым словом, с точкой, далее — обычный текст пересказа).

### ШАГ 1.6. Ночной звонок

Проверь время звонка (поле «Время» из исходных данных). Если звонок совершён **с 22:01 до 8:59** — заполни `is_night_call = true`.

**Если звонок ночной, в начале `dialog_overview` добавь маркер «НОЧНОЙ.»** Если звонок одновременно целевой и ночной — оба маркера идут в начале через точку, в порядке: `НОЧНОЙ. Целевой. <дальше обычный пересказ>`.



### ШАГ 2. Оценка по категориям (максимум 43 балла)

Для каждой категории укажи `score` и `comment` с обоснованием на основе реплик из диалога. Если ты применяешь смягчённый стандарт из-за типа звонка (например, «продажа» неприменима на инфозвонке) — обязательно заполни `exemption_justification` цитатой, подтверждающей это. Пустое или общее обоснование («это был информационный звонок») не принимается — нужна конкретная реплика клиента/администратора.

**1. Установление контакта (max 2)**
0 — нет приветствия/представления; 1 — есть, но формально/сухо; 2 — тёплое приветствие, представление клиники и себя.

**2. Использование имени клиента (max 2)**
Оценивается **фактическое использование**, а не автоматически. 0 — имя не использовано, хотя клиент представился; 1 — использовано один раз; 2 — использовано естественно несколько раз по ходу диалога. Если клиент вообще не назвал имя и администратор не мог его уточнить — ставь 2 и обязательно укажи это в `comment` (это единственный легитимный случай не снижать балл, и он должен быть подтверждён — клиент не называл себя, а не «администратор не спросил»).

**3. Выяснение потребностей (max 9)**
Уточняющие вопросы о пациенте (вид, возраст, симптомы, история), цель обращения, срочность. Для `информационный`/`лаборатория_или_курьер`/`текущий_пациент` — оценивай, задал ли администратор уместные для контекста уточняющие вопросы, а не полный сбор анамнеза. Балл ниже 5 — если не заданы даже базовые уточнения там, где они были возможны.

**4. Презентация услуг (max 7)**
Понятное, структурированное объяснение услуги/процедуры, преимуществ, что входит. Для `лаборатория_или_курьер` категория оценивается по чёткости и полноте передачи служебной информации, а не по продающей подаче.

**5. Презентация цены (max 3)**
Названа ли цена ясно, без запинок, с расшифровкой, если клиент спрашивает «за что». **Обязательное условие: администратор должен проинформировать клиента о скидке 2% при оплате наличными.** Это правило применяется к любому звонку, где называется цена или обсуждается способ оплаты — независимо от типа звонка (продающий, информационный и т.д.).
- 3 балла — цена названа понятно **и** клиент проинформирован о скидке за наличную оплату
- 2 балла — цена названа понятно, но о скидке за наличные не упомянуто
- 0–1 балл — цена названа нечётко/с запинками, и/или скидка не упомянута
Если в звонке цена вообще не обсуждалась (например, чисто информационный запрос без вопроса о стоимости) — ставь `not_applicable: true` вместо score.

**6. Работа с возражением (max 4)**
Даже при обоснованном отказе (`отказ`) администратор должен: (а) выслушать и не спорить, (б) уточнить причину, (в) предложить альтернативу или зафиксировать причину для базы. Балл 4 — если сделаны все три пункта несмотря на отказ клиента. Балл ниже — если администратор просто согласился с отказом и завершил разговор без попытки уточнить/предложить альтернативу. **Сам факт отказа клиента не даёт автоматически максимум — оценивается реакция администратора.**

**7. Ведение к записи (max 8)**
Была ли предпринята попытка перевести разговор в запись или следующий шаг, уместный для типа звонка (для `информационный` — это может быть предложение «Хотите, я сразу запишу на консультацию?», для `текущий_пациент` — предложение повторного визита при необходимости). Балл 8 — если такая попытка сделана и доведена до конкретики (дата/время/врач предложены). Балл 0–2 — если администратор не сделал даже одной попытки предложить следующий шаг там, где это было уместно. Для `лаборатория_или_курьер` категория не оценивается по продаже — ставь 8 и помечай `not_applicable: true`, если запись объективно не имеет смысла в этом типе звонка (это единственная категория, где допустим отдельный флаг неприменимости вместо оценки).

**8. Завершение диалога (max 3)**
Резюме договорённостей, прощание, приглашение обращаться повторно.

**9. Индивидуальный подход (max 5)**
Учёт истории пациента/клиента, эмпатия, неформальные детали (уточнил про самочувствие питомца ранее и т.п.).

### ШАГ 3. Критические нарушения (override)

Отдельно от баллов зафиксируй в массиве `critical_flags` любые из следующих нарушений, если они были:
- `грубость_или_неуважение`
- `некорректная_медицинская_информация` (в т.ч. советы, которые должен давать только врач)
- `нарушение_конфиденциальности` (разглашение чужих данных)
- `отказ_без_попытки_помочь`
- `явная_ошибка_в_записи` (перепутана дата/врач/услуга)

**Если `critical_flags` не пуст — итоговая категория не может быть выше 3, независимо от суммы баллов.** Поясни каждый флаг конкретной цитатой в `critical_flags_evidence`.

### ШАГ 3.5. Обязательная проверка: скидка за наличную оплату

Отдельно от баллов зафиксируй в поле `cash_discount_mentioned` (true/false/null):
- `true` — администратор сообщил клиенту о скидке 2% при оплате наличными
- `false` — цена/оплата обсуждались, но о скидке не сказали
- `null` — цена и способ оплаты в звонке не обсуждались (правило неприменимо)

Это обязательное правило клиники: **о скидке 2% за наличную оплату нужно предупреждать всех владельцев**, у которых по ходу разговора всплывает цена или способ оплаты — вне зависимости от того, записывается клиент или нет. Если `cash_discount_mentioned = false` — это влияет на балл категории «Презентация цены» (см. выше), но не создаёт critical_flag: это скорее пропуск полезной информации, чем грубая ошибка.

### ШАГ 4. Обязательный блок при состоявшейся записи

Если `appointment_made = true`, **обязательно** заполни `appointment_details`:
```
"appointment_details": {{
  "date": "дата приёма, если названа, иначе null",
  "time": "время, если названо, иначе null",
  "doctor_or_service": "врач и/или услуга, на которую записан клиент",
  "patient_name": "имя питомца, если известно",
  "client_name": "имя клиента, если известно"
}}
```
Если `appointment_made = false`, поле должно быть `null`.

### Итоговые категории

Пересчитаны на основе распределения реальных данных — прежние границы (37/30/23) почти не разделяли звонки, поэтому проверь фактическое распределение после нескольких недель использования новой методики и при необходимости скорректируй пороги. Стартовые границы:

1 — отлично: 37–43
2 — хорошо: 28–36
3 — удовлетворительно: 18–27
4 — плохо: ≤17 **или** непустой `critical_flags`

---

### ФОРМАТ ОТВЕТА — строго JSON, без markdown и лишнего текста:

```json
{{
  "raw_call_greeting": "Точный дословный текст приветствия",
  "admin_name": "Имя Фамилия администратора",
  "call_type": "продающий / информационный / лаборатория_или_курьер / текущий_пациент / отказ",
  "call_type_evidence": "Цитата, подтверждающая классификацию",
  "call_target": "целевой / нецелевой",
  "call_target_evidence": "Цитата или краткое пояснение",
  "is_night_call": false,
  "appointment_made": true,
  "cash_discount_mentioned": null,
  "appointment_details": {{
    "date": null,
    "time": null,
    "doctor_or_service": null,
    "patient_name": null,
    "client_name": null
  }},
  "total_score": 0,
  "category": 0,
  "critical_flags": [],
  "critical_flags_evidence": "",
  "dialog_overview": "Подробный пересказ ключевых моментов диалога. Начинается с маркеров «НОЧНОЙ.» и/или «Целевой.», если применимо",
  "details": {{
    "contact": {{"score": 0, "comment": ""}},
    "name_usage": {{"score": 0, "comment": ""}},
    "needs_analysis": {{"score": 0, "comment": ""}},
    "presentation": {{"score": 0, "comment": "", "exemption_justification": ""}},
    "pricing": {{"score": 0, "comment": ""}},
    "objections": {{"score": 0, "comment": "", "exemption_justification": ""}},
    "closing_to_appointment": {{"score": 0, "comment": "", "exemption_justification": "", "not_applicable": false}},
    "termination": {{"score": 0, "comment": ""}},
    "individual_approach": {{"score": 0, "comment": ""}}
  }}
}}
```

---
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
                        return content_text
                    except Exception:
                        cleaned = content_text.replace("```json", "").replace("```", "").strip()
                        return cleaned

        except Exception as retry_exc:
            logger.error(f"❌ [OPENROUTER FAILED] Все попытки исчерпаны. Не удалось получить ответ: {retry_exc}")
            return None

    except Exception as e:
        logger.error(f"❌ Крах прямого OpenRouter запроса: {e}")
        return None
