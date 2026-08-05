import time
import os
import shutil
from datetime import datetime
from utils.logs import setup_logger

# Импортируем ВСЕ хелперы для БД в одном месте
from src.database import (
    init_db, clear_old_data,
    get_or_set_period_start, reset_period, get_success_calls_for_master,
    get_all_call_events
)
from src.api_client import get_calls
from src.excel_maker import update_master_report, update_daily_report, update_statistics_report, BASE_REPORTS_DIR
from src.call import process_single_call
from src.sync_manager import YandexFolderSyncer

logger = setup_logger("watchdog")

PERIOD_DAYS = 30
PERIOD_SECONDS = PERIOD_DAYS * 24 * 3600
CHECK_WINDOW_FIRST_RUN_HOURS = 24
CHECK_WINDOW_NORMAL_MINUTES = 20
SLEEP_BETWEEN_CALLS = 8
SLEEP_BETWEEN_CYCLES = 180


def watchdog():
    init_db()
    logger.info(">>> Watchdog process started. Monitoring Novofon calls...")

    processed_today = 0
    is_first_run = True

    while True:
        current_time = time.time()

        # Проверка 30-дневного периода
        period_start = get_or_set_period_start()
        if current_time - period_start > PERIOD_SECONDS:
            logger.warning("⏳ [GEONOCIDE] Срок периода истек. Начинаем зачистку...")
            if os.path.exists(BASE_REPORTS_DIR):
                try:
                    shutil.rmtree(BASE_REPORTS_DIR)
                    logger.info("✨ [GEONOCIDE] Папка reports успешно удалена.")
                except Exception as e:
                    logger.error(f"✗ [GEONOCIDE] Не удалось удалить папку: {e}")
            
            reset_period()
            clear_old_data(days=PERIOD_DAYS)
            processed_today = 0

        try:
            hours_to_check = CHECK_WINDOW_FIRST_RUN_HOURS if is_first_run else (CHECK_WINDOW_NORMAL_MINUTES / 60)
            
            if is_first_run:
                logger.info(f"⏳ Первый запуск. Проверяем за {hours_to_check} часа...")
            else:
                logger.debug(f"Проверяем последние {CHECK_WINDOW_NORMAL_MINUTES} минут...")

            calls = get_calls(hours_back=hours_to_check)
            is_first_run = False  # надёжнее сбрасывать сразу

            calls = list(reversed(calls))  # от старых к новым — оставляем

            new_found = 0
            master_needs_update = False

            for call in calls:
                result = process_single_call(call)
                if result == "processed":
                    new_found += 1
                    processed_today += 1
                    master_needs_update = True
                elif result == "skipped":
                    continue
                # "error" — просто продолжаем

            if master_needs_update:
                # Получаем все успешные звонки из базы данных
                calls_for_report = get_success_calls_for_master()
                
                # 1. Обновляем глобальный Мастер-Отчет
                master_path = update_master_report(calls_for_report)
                if master_path:
                    logger.info(f"| [MASTER EXCEL] Сводный мастер-отчет обновлён: {master_path}")
                
                # 2. === НАШ НОВЫЙ ВЫЗОВ: Обновляем Ежедневный отчет за сегодня ===
                try:
                    daily_path = update_daily_report(calls_for_report)
                    if daily_path:
                        logger.info(f"| [DAILY EXCEL] Ежедневный отчет за сегодня создан/обновлен: {daily_path}")
                except Exception as daily_err:
                    logger.error(f"❌ Сбой при создании ежедневного отчета: {daily_err}", exc_info=True)

                # 3. === ЕДИНЫЙ НАКОПИТЕЛЬНЫЙ ФАЙЛ СТАТИСТИКИ ПО ВНУТРЕННИМ НОМЕРАМ (как мастер-отчёт) ===
                try:
                    all_events = get_all_call_events()
                    stats_path = update_statistics_report(all_events)
                    if stats_path:
                        logger.info(f"| [STATS EXCEL] Статистика по внутренним номерам обновлена: {stats_path}")
                except Exception as stats_err:
                    logger.error(f"❌ Сбой при создании файла статистики: {stats_err}", exc_info=True)
                
                # 4. === ФУЛЛ КОММИТ ПАПКИ REPORTS НА ДИСК (СИНКЛЕР) ===
                try:
                    syncer = YandexFolderSyncer()
                    syncer.sync_reports()
                except Exception as sync_err:
                    logger.error(f"❌ Критический сбой модуля синхронизации: {sync_err}")

            if new_found == 0:
                logger.debug("○ Новых звонков не найдено")

        except Exception as e:
            logger.error(f"[CRITICAL] Watchdog loop crashed: {e}", exc_info=True)
            time.sleep(60)
            continue

        time.sleep(SLEEP_BETWEEN_CYCLES)

if __name__ == "__main__":
    watchdog()
