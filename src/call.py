import json
import time
import re

from utils.logs import setup_logger

from src.api_client import get_record, analyze_audio
from src.excel_maker import create_call_report
from src.database import is_call_processed, save_analysis_to_db

import config as cfg

logger = setup_logger("watchdog")

UNKNOWN_VALUE = "Не определен"

def generate_call_key(call: dict) -> str:
    return f"{call['id']}_{call['record_id']}"

def process_single_call(call: dict) -> str:
    """Обрабатывает один звонок. Возвращает: 'processed', 'skipped', 'error'"""
    try:
        call_id = str(call.get('id'))
        logger.debug(f"Проверка звонка {call_id}")

        record_id = call.get('record_id')
        if isinstance(record_id, dict) and 'id' in record_id:
            record_id = record_id.get('id')

        talk_duration = int(call.get('talk_duration') or call.get('duration') or 0)

        if not record_id:
            logger.debug(f"| [SKIP] Звонок {call_id} — нет record_id")
            return "skipped"

        if talk_duration < 7:
            logger.debug(f"| [SKIP] Звонок {call_id} — слишком короткий ({talk_duration} сек)")
            return "skipped"

        if call.get('is_lost') and talk_duration == 0:
            logger.debug(f"| [SKIP] Звонок {call_id} — потерянный")
            return "skipped"

        call['record_id'] = str(record_id)
        call_key = generate_call_key(call)

        if is_call_processed(call_key):
            logger.debug(f"○ Уже обработан: {call_key}")
            return "skipped"

        # Фильтр по виртуальным номерам
        virtual_num = str(call.get('virtual_phone_number') or '').strip()
        if virtual_num:
            # Вычищаем всё кроме цифр из номера АТС: '74951283548' -> '74951283548'
            clean_virtual = re.sub(r'\D', '', virtual_num)

            # Вычищаем всё кроме цифр из твоего черного списка в конфиге
            clean_block_list = [re.sub(r'\D', '', str(num)) for num in cfg.BLOCK_NUMBERS]

            if clean_virtual in clean_block_list:
                logger.info(f"| [SKIP] Отфильтровано BLOCK_NUMBERS: {virtual_num}")
                return "skipped"

        logger.info(f"→ Новый звонок для анализа: {call_id} | {call.get('phone')} | {talk_duration} сек.")

        # Скачивание записи
        audio = get_record(call_id, record_id, call.get('communication_id'))
        if not audio:
            logger.warning(f"✗ Не удалось скачать запись {call_id}")
            record_url = f"https://app.novofon.ru/system/media/talk/{call.get('communication_id') or call_id}/{record_id}/"
            save_analysis_to_db(
                call_key=call_key, call_id=call_id, record_id=record_id,
                start_time=call.get('start_time'), duration=talk_duration,
                phone=call.get('contact_phone_number'), analysis_text=None, status="download_failed",
                record_url=record_url, admin_name=UNKNOWN_VALUE, clinic_branch=UNKNOWN_VALUE
            )
            return "error"

        # Анализ через Gemini (с повторами)
        analysis_json = None
        for attempt in range(3):
            try:
                logger.info(f"⚡ Gemini анализ (попытка {attempt+1}/3): {call_id}")
                raw_response = analyze_audio(audio, call)
                
                if not raw_response:
                    raise Exception("Пустой ответ от analyze_audio")
                
                # Защита: проверяем, не вернулась ли ошибка вместо валидного отчета
                try:
                    check_error = json.loads(raw_response)
                    if "error" in check_error:
                        error_msg = check_error["error"]
                        logger.warning(f"⚠ Обнаружена ошибка API в ответе Gemini: {error_msg}")
                        
                        # Если поймали рейт-лимит (429), спим дольше, чтобы проскочить окно
                        if "429" in str(error_msg) or "quota" in str(error_msg).lower():
                            logger.info("⏳ Превышена квота (429). Засыпаем на 25 секунд...")
                            time.sleep(25)
                        raise Exception(f"Gemini API Error: {error_msg}")
                except json.JSONDecodeError:
                    # Если это не JSON с ошибкой, а обычная строка/валидный отчет — всё ок, выходим из try
                    pass

                # Если проверка пройдена, сохраняем результат
                analysis_json = raw_response
                break

            except Exception as e:
                logger.warning(f"Попытка {attempt+1} не удалась: {e}")
                if attempt < 2:
                    # Если это не 429, которая отработала выше, даем стандартную паузу
                    time.sleep(10 if "503" in str(e) else 5)

        if not analysis_json:

            logger.error(f"✗ Анализ не удался после 3 попыток: {call_id}")

            customer_phone = call.get('contact_phone_number') or call.get('phone') or "Не определен"

            record_url = f"https://app.novofon.ru/system/media/talk/{call.get('communication_id') or call_id}/{record_id}/"

            save_analysis_to_db(
                call_key=call_key, call_id=call_id, record_id=record_id,
                start_time=call.get('start_time'), duration=talk_duration,
                phone=customer_phone, analysis_text=analysis_json,
                status="success", record_url=record_url,
                admin_name=admin_name, clinic_branch=clinic_branch
            )

            return "error"

        # === Парсинг новых полей ===
        try:
            parsed = json.loads(analysis_json)
            admin_name = parsed.get("admin_name", UNKNOWN_VALUE)
            clinic_branch = parsed.get("clinic_branch", UNKNOWN_VALUE)
        except:
            parsed = {}
            admin_name = clinic_branch = UNKNOWN_VALUE

        customer_phone = call.get('contact_phone_number') or call.get('phone') or "Не определен"

        record_url = f"https://app.novofon.ru/system/media/talk/{call.get('communication_id') or call_id}/{record_id}/"

        save_analysis_to_db(
            call_key=call_key, call_id=call_id, record_id=record_id,
            start_time=call.get('start_time'), duration=talk_duration,
            phone=customer_phone, analysis_text=analysis_json,
            status="success", record_url=record_url,
            admin_name=admin_name, clinic_branch=clinic_branch
        )

        # Создаём индивидуальный отчёт
        path_to_excel = create_call_report(
            gemini_json_str=analysis_json,
            call_id=call_id,
            duration_sec=talk_duration,
            record_url=record_url,
            customer_phone=customer_phone
        )

        if path_to_excel:
            logger.info(f"| [EXCEL] Создан индивидуальный отчёт: {path_to_excel}")

        logger.info(f"✅ Успешно обработан: {call_id} | Админ: {admin_name}")
        return "processed"

    except Exception as e:
        logger.error(f"✗ Критическая ошибка обработки звонка {call.get('id')}: {e}", exc_info=True)
        return "error"