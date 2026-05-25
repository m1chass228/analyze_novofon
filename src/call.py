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

def clean_to_10_digits(phone_number) -> str:
    """Очищает номер от любого мусора и возвращает последние 10 цифр для надежного сравнения"""
    if not phone_number:
        return ""
    digits = re.sub(r'\D', '', str(phone_number))
    return digits[-10:] if len(digits) >= 10 else digits

def process_single_call(call: dict) -> str:
    """Обрабатывает один звонок. Возвращает: 'processed', 'skipped', 'error'"""
    try:
        call_id = str(call.get('id'))
        logger.debug(f"Проверка звонка {call_id}")

        record_id = call.get('record_id')
        if isinstance(record_id, dict) and 'id' in record_id:
            record_id = record_id.get('id')

        talk_duration = int(call.get('talk_duration') or call.get('duration') or 0)
        direction = call.get('direction', 'in')

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

        # === ФИЛЬТРАЦИЯ BLOCK_NUMBERS ===
        virtual_num = call.get('virtual_phone_number')
        if virtual_num:
            clean_virtual = clean_to_10_digits(virtual_num)
            clean_block_list = [clean_to_10_digits(num) for num in getattr(cfg, 'BLOCK_NUMBERS', []) if num]

            if clean_virtual and clean_virtual in clean_block_list:
                logger.info(f"| [SKIP] Отфильтровано BLOCK_NUMBERS. Виртуальный: {virtual_num} (сравнение по {clean_virtual})")
                return "skipped"

        logger.info(f"→ Новый звонок для анализа: {call_id} | {call.get('phone')} | {talk_duration} сек. | Направление: {direction}")

        # Скачивание записи
        audio = get_record(call_id, record_id, call.get('communication_id'))
        if not audio:
            logger.warning(f"✗ Не удалось скачать запись {call_id}")
            record_url = f"https://app.novofon.ru/system/media/talk/{call.get('communication_id') or call_id}/{record_id}/"
            save_analysis_to_db(
                call_key=call_key, call_id=call_id, record_id=record_id,
                start_time=call.get('start_time'), duration=talk_duration,
                phone=call.get('contact_phone_number'), analysis_text=None, status="download_failed",
                record_url=record_url, admin_name=UNKNOWN_VALUE, clinic_branch=UNKNOWN_VALUE,
                direction=direction
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
                
                try:
                    check_error = json.loads(raw_response)
                    if "error" in check_error:
                        error_msg = check_error["error"]
                        logger.warning(f"⚠ Обнаружена ошибка API в ответе Gemini: {error_msg}")
                        
                        if "429" in str(error_msg) or "quota" in str(error_msg).lower():
                            logger.info("⏳ Превышена квота (429). Засыпаем на 25 секунд...")
                            time.sleep(60)
                        raise Exception(f"Gemini API Error: {error_msg}")
                except json.JSONDecodeError:
                    pass
                        
                analysis_json = raw_response
                break

            except Exception as e:
                logger.warning(f"Попытка {attempt+1} не удалась: {e}")
                if attempt < 2:
                    time.sleep(10 if "503" in str(e) else 5)

        customer_phone = call.get('contact_phone_number') or call.get('phone') or UNKNOWN_VALUE
        record_url = f"https://app.novofon.ru/system/media/talk/{call.get('communication_id') or call_id}/{record_id}/"
        novofon_device = call.get('admin_name') or call.get('employee_name') or call.get('responsible_ear_name') or UNKNOWN_VALUE

        # Если анализ провалился после всех попыток
        if not analysis_json:
            logger.error(f"✗ Анализ не удался после 3 попыток: {call_id}")
            save_analysis_to_db(
                call_key=call_key, call_id=call_id, record_id=record_id,
                start_time=call.get('start_time'), duration=talk_duration,
                phone=customer_phone, analysis_text=None,
                status="gemini_failed", record_url=record_url,
                admin_name=novofon_device, clinic_branch=UNKNOWN_VALUE,
                direction=direction
            )
            return "error"

        # === УМНЫЙ МЭТЧИНГ ИМЕНИ ДЛЯ БАЗЫ И ОТЧЕТОВ ===
        clinic_branch = UNKNOWN_VALUE
        final_admin_display = novofon_device

        try:
            parsed = json.loads(analysis_json)
            clinic_branch = parsed.get("clinic_branch", UNKNOWN_VALUE)
            ai_extracted_name = parsed.get("admin_name", "").strip()
            
            if ai_extracted_name and ai_extracted_name not in ["Не определен", "Не представился(-ась)"]:
                if novofon_device == UNKNOWN_VALUE:
                    final_admin_display = ai_extracted_name
                elif ai_extracted_name.lower() in str(novofon_device).lower():
                    final_admin_display = novofon_device
                else:
                    final_admin_display = f"{novofon_device} ({ai_extracted_name})"
            
            # Зашиваем красивое имя обратно в JSON, чтобы индивидуальный отчет съел его автоматически
            parsed["admin_name"] = final_admin_display
            analysis_json = json.dumps(parsed, ensure_ascii=False)

        except Exception as parse_err:
            logger.warning(f"Ошибка кастомизации JSON полей: {parse_err}")

        # Сохраняем успешный анализ звонка в БД
        save_analysis_to_db(
            call_key=call_key, call_id=call_id, record_id=record_id,
            start_time=call.get('start_time'), duration=talk_duration,
            phone=customer_phone, analysis_text=analysis_json,
            status="success", record_url=record_url,
            admin_name=final_admin_display, clinic_branch=clinic_branch,
            direction=direction
        )

        # Создаём индивидуальный отчет (передаем время звонка и направление)
        path_to_excel = create_call_report(
            gemini_json_str=analysis_json,
            call_id=call_id,
            duration_sec=talk_duration,
            record_url=record_url,
            customer_phone=customer_phone,
            call_start_time=call.get('start_time'),
            direction=direction
        )

        if path_to_excel:
            logger.info(f"| [EXCEL] Создан индивидуальный отчёт: {path_to_excel}")

        logger.info(f"✅ Успешно обработан: {call_id} | Админ: {final_admin_display} | Направление: {direction}")
        return "processed"

    except Exception as e:
        logger.error(f"✗ Критическая ошибка обработки звонка {call.get('id')}: {e}", exc_info=True)
        return "error"