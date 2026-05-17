import json
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
        call_id = call.get('id')
        logger.debug(f"Проверка звонка {call_id}")

        # Пропускаем без разговора
        if (call.get('is_lost') or 
            int(call.get('talk_duration') or 0) == 0 or 
            not call.get('call_records')):
            logger.debug(f"| [SKIP] Звонок {call_id} — без записи")
            return "skipped"

        # Получаем record_id
        records = call.get('call_records', [])
        record_id = call.get('record_id') or (records[0] if records else None)
        
        if isinstance(record_id, dict) and 'id' in record_id:   # на всякий случай
            record_id = record_id['id']
        
        if not record_id:
            logger.warning(f"| [SKIP] Нет record_id у звонка {call_id}")
            return "skipped"

        call['record_id'] = record_id
        call_key = generate_call_key(call)

        if is_call_processed(call_key):
            logger.debug(f"○ Уже обработан: {call_key}")
            return "skipped"

        # Фильтр по номеру
        virtual_num = str(call.get('virtual_phone_number') or '').strip()
        if any(str(i).strip() == virtual_num for i in cfg.BLOCK_NUMBERS if str(i).strip()):
            logger.info(f"| [SKIP] Отфильтровано по BLOCK_NUMBERS: {virtual_num} (ID: {call_id})")
            return "skipped"

        logger.info(f"→ Новый звонок: {call_id} | {call.get('phone')} | {call.get('duration')} сек.")

        audio = get_record(call['id'], record_id, call.get('communication_id'))
        if not audio:
            logger.warning(f"✗ Не удалось скачать запись {call_id}")
            save_analysis_to_db(
                call_key=call_key, call_id=call_id, record_id=record_id,
                start_time=call.get('start_time'), duration=call.get('duration'),
                phone=call.get('phone'), analysis_text=None, status="download_failed",
                record_url=f"https://app.novofon.ru/system/media/talk/{call.get('communication_id')}/{record_id}/"
            )
            return "error"

        logger.info(f"⚡ Анализируем через Gemini: {call_id}")
        analysis_json = analyze_audio(audio, call)

        if not analysis_json:
            logger.error(f"✗ Анализ не удался: {call_id}")
            return "error"

        # Парсим JSON один раз
        try:
            parsed = json.loads(analysis_json)
            admin_name = parsed.get("admin_name", UNKNOWN_VALUE)
            clinic_branch = parsed.get("clinic_branch", UNKNOWN_VALUE)
        except:
            admin_name = clinic_branch = UNKNOWN_VALUE
            parsed = {}

        record_url = f"https://app.novofon.ru/system/media/talk/{call.get('communication_id')}/{record_id}/"

        save_analysis_to_db(
            call_key=call_key, call_id=call_id, record_id=record_id,
            start_time=call.get('start_time'), duration=call.get('duration'),
            phone=call.get('phone'), analysis_text=analysis_json, 
            status="success", record_url=record_url,
            admin_name=admin_name, clinic_branch=clinic_branch
        )

        # Создаём Excel
        path_to_excel = create_call_report(
            gemini_json_str=analysis_json, 
            call_id=str(call_id), 
            duration_sec=int(call.get('duration') or 0), 
            record_url=record_url
        )

        if path_to_excel:
            logger.info(f"| [EXCEL] Создан: {path_to_excel}")
        else:
            logger.warning(f"| [EXCEL] Не удалось создать отчёт для {call_id}")

        logger.info(f"✓ Успешно обработан: {call_id}")
        return "processed"

    except Exception as e:
        logger.error(f"✗ Критическая ошибка при обработке звонка {call.get('id')}: {e}", exc_info=True)
        return "error"
    
if __name__ == "__main__":
    process_single_call()