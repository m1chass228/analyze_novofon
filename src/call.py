
import json
import re

from utils.logs import setup_logger

from src.api_client import get_record, analyze_audio
from src.excel_maker import create_call_report
from src.database import is_call_processed, save_analysis_to_db
from utils.is_audio_empty import is_audio_empty

import time

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

        # =====================================================================
        # 🚫 1. ФИЛЬТР: ВИРТУАЛЬНЫЙ НОМЕР (ИЗ КОНФИГА)
        # =====================================================================
        virtual_phone = str(call.get('virtual_phone_number') or call.get('did') or call.get('destination') or '').strip()
        # Очищаем от лишних символов, если они есть (например, +, пробелы, скобки)
        cleaned_virtual = re.sub(r'\D', '', virtual_phone)

        # Подгружаем черный список номеров из конфига (по дефолту пустой список, если забыл указать)
        blocked_numbers = getattr(cfg, 'BLOCK_NUMBERS', [])
        
        # Проверяем, совпадает ли конец номера или содержится ли он в списке
        for b_num in blocked_numbers:
            b_num_clean = re.sub(r'\D', '', str(b_num))
            if b_num_clean and (b_num_clean in cleaned_virtual or cleaned_virtual.endswith(b_num_clean)):
                logger.info(f"🚫 [BLACKLIST] Звонок {call_id} пропущен: виртуальный номер {virtual_phone} в черном списке конфига.")
                return "skipped"

        # =====================================================================
        # 🚫 2. ФИЛЬТР: АДМИНИСТРАТОР (ИЗ КОНФИГА)
        # =====================================================================
        employees = call.get('employees', [])
        emp_name = employees[0].get('employee_full_name', '') if employees else ''
        
        novofon_admin = str(
            call.get('admin_name') or 
            call.get('first_answered_employee_full_name') or 
            call.get('last_answered_employee_full_name') or 
            emp_name or ''
        ).strip().lower()

        blocked_admins = getattr(cfg, 'BLOCK_ADMINS', [])
        
        if any(b_admin.strip().lower() in novofon_admin for b_admin in blocked_admins if b_admin):
            logger.info(f"🚫 [BLACKLIST] Звонок {call_id} пропущен: администратор '{novofon_admin}' забанен в конфиге.")
            return "skipped"

        record_id = call.get('record_id')
        if isinstance(record_id, dict) and 'id' in record_id:
            record_id = record_id.get('id')

        talk_duration = int(call.get('talk_duration') or call.get('duration') or 0)
        direction = call.get('direction', 'in')

        if not record_id:
            logger.debug(f"○ Пропущен звонок {call_id}: нет record_id (нет записи разговора)")
            return "skipped"

        if talk_duration < getattr(cfg, 'MIN_CALL_DURATION_SEC', 25):
            logger.debug(f"○ Пропущен звонок {call_id}: длительность {talk_duration} сек. меньше лимита")
            return "skipped"

        call_key = generate_call_key(call)
        if is_call_processed(call_key):
            logger.debug(f"○ Пропущен звонок {call_id}: уже обработан ранее (key: {call_key})")
            return "skipped"

        admin_name = call.get('admin_name') or UNKNOWN_VALUE
        customer_phone = call.get('phone') or UNKNOWN_VALUE

        logger.info(f"→ Новый звонок для анализа: {call_id} | {customer_phone} | {talk_duration} сек. | Направление: {direction}")

        # === Скачиваем аудиофайл звонка ===
        audio = get_record(call_id, record_id, call.get('communication_id'))
        if not audio:
            logger.warning(f"❌ Не удалось скачать аудио для звонка {call_id}")
            return "error"
        
        # === Проверяем на пустоту ===
        if is_audio_empty(audio):
            logger.info(f"⏭️ Звонок {call_id} пропущен: в записи только гудки или тишина.")
            
            # Сохраняем в базу со статусом skipped, чтобы больше не дергать
            save_analysis_to_db(
                call_key=call_key, call_id=call_id, record_id=record_id,
                start_time=call.get('start_time'), duration=talk_duration,
                phone=customer_phone, analysis_text="{}",
                status="skipped", record_url="",
                admin_name=admin_name, clinic_branch=UNKNOWN_VALUE,
                direction=direction
            )
            return "skipped"

        # === Отправляем аудио на анализ диспетчеру (он сам решит: бесплатный GAS или платный fallback) ===
        logger.info(f"⚡ Gemini анализ (попытка 1/3): {call_id}")
        raw_response = analyze_audio(audio, call)

        if not raw_response:
            logger.error(f"❌ Все попытки анализа звонка {call_id} завершились неудачей (вернулся None)")
            return "error"

<<<<<<< Updated upstream
        # === ПАРСИНГ ОТВЕТА ===
        clinic_branch = UNKNOWN_VALUE
        final_admin_display = admin_name
        analysis_json = raw_response
=======
        logger.info(f"⚡ Анализируем через Gemini: {call_id}")
        # === ДОБАВЛЯЕМ СЮДА ЦИКЛ ПОПЫТОК ДЛЯ СТАБИЛЬНОСТИ ===
        max_attempts = 3
        attempt = 0
        analysis_json = None

        while attempt < max_attempts:
            try:
                analysis_json = analyze_audio(audio, call)
                if analysis_json:
                    break  # Успешно получили ответ — выходим из цикла
            except Exception as e:
                logger.warning(f"⚠️ Ошибка Gemini на попытке {attempt+1}: {e}")
            
            attempt += 1
            if attempt < max_attempts:
                time.sleep(5) # Ждем перед повтором, если API штормит
>>>>>>> Stashed changes

        try:
            parsed = json.loads(raw_response)
            
            # Извлекаем базовые поля отчета
            clinic_branch = parsed.get("clinic_branch")
            if not clinic_branch or clinic_branch == "null": 
                clinic_branch = UNKNOWN_VALUE

            # === Красивая склейка имени администратора ===
            # 1. Берем имя линии из Новофона (переменная admin_name из начала функции)
            novofon_tube = admin_name.strip() if admin_name else ""
            if novofon_tube in ["Не определен", "UNKNOWN_VALUE", "null", "None"]:
                novofon_tube = ""

            # 2. Вытаскиваем имя, которое нашла нейронка
            gemini_admin = parsed.get("admin_name", "").strip()
            if gemini_admin in ["Не представился(-ась)", "Не определен", "null", "None"]:
                gemini_admin = ""

            # 3. Скрепляем строго по формату: admin_full_name (имя вытащенное нейронкой)
            if novofon_tube and gemini_admin:
                final_admin_display = f"{novofon_tube} ({gemini_admin})"  # Оба есть -> "Marjino 120 (Анастасия)"
            elif novofon_tube:
                final_admin_display = novofon_tube                        # Только Новофон -> "Marjino 120"
            elif gemini_admin:
                final_admin_display = gemini_admin                        # Только нейронка -> "Анастасия"
            else:
                final_admin_display = "Не определен"                      # Если вообще пусто с обеих сторон (крайний случай)

            # Перезаписываем в JSON, чтобы excel_maker сразу съел готовую строку
            parsed["admin_name"] = final_admin_display
            analysis_json = json.dumps(parsed, ensure_ascii=False)

        except Exception as parse_err:
            logger.warning(f"Ошибка кастомизации JSON полей: {parse_err}")

        # Сохраняем успешный анализ звонка в БД
        record_url = f"https://app.novofon.ru/system/media/talk/{call.get('communication_id', call_id)}/{record_id}/"
        save_analysis_to_db(
            call_key=call_key, call_id=call_id, record_id=record_id,
            start_time=call.get('start_time'), duration=talk_duration,
            phone=customer_phone, analysis_text=analysis_json,
            status="success", record_url=record_url,
            admin_name=final_admin_display, clinic_branch=clinic_branch,
            direction=direction
        )

        # Создаём индивидуальный отчет
        path_to_excel = create_call_report(
            gemini_json_str=analysis_json,
            call_id=call_id,
            duration_sec=talk_duration,
            record_url=record_url,
            customer_phone=customer_phone,
            call_start_time=call.get('start_time'),
            direction=direction
        )

        # === ИНТЕГРАЦИЯ ЯНДЕКС ДИСКА ===
        if path_to_excel:
            logger.info(f"| [EXCEL] Создан локально: {path_to_excel}")
            
        # Инициализируем загрузчик (токен лучше вынести в config)
            from src.yandex_disk_uploader import YandexDiskUploader
            import config as cfg
        
            uploader = YandexDiskUploader(token=cfg.YANDEX_TOKEN, remote_base_path="/NovofonReports")
        
            # Загружаем индивидуальный отчет в подпапку 'individual'
            uploader.upload_file(path_to_excel, subfolder_name="individual")

        if path_to_excel:
            logger.info(f"| [EXCEL] Создан индивидуальный отчёт: {path_to_excel}")

        logger.info(f"✅ Успешно обработан: {call_id} | Админ: {final_admin_display} | Направление: {direction}")
        return "processed"

    except Exception as e:
        logger.error(f"❌ Ошибка при обработке звонка {call.get('id', 'unknown')}: {e}", exc_info=True)
        return "error"
