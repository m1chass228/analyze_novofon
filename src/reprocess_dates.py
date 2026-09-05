"""
Разовый скрипт: сбрасывает и заново обрабатывает звонки за указанный диапазон дат.

Использование (с сервера, из корня проекта /root/analyze_novofon):

    python3 -m src.reprocess_dates 2026-09-03 2026-09-06

Первая дата — начало диапазона (включительно), вторая — КОНЕЦ диапазона
(НЕ включительно), т.е. для "переобработать 03, 04 и 05 сентября" нужно
указать 2026-09-03 и 2026-09-06 (граница на начало следующего дня).

Что делает:
1. Удаляет из БД записи processed_calls и call_events за этот диапазон —
   иначе is_call_processed() скажет "уже обработано" и звонки пропустятся.
2. Запрашивает у Новофона ВСЕ звонки за диапазон (с пагинацией).
3. Прогоняет каждый через process_single_call() — то есть заново скачивает
   аудио, заново шлёт на анализ (уже через исправленный api_client.py и
   пополненный баланс OpenRouter), пересоздаёт индивидуальные отчёты.
4. В конце пересобирает master/daily/statistics/dashboard и запускает
   синхронизацию с Яндекс.Диском.

ВНИМАНИЕ: это реально заново тратит деньги на OpenRouter за каждый звонок
в диапазоне (не только "пустые") — если нужно переобработать ТОЛЬКО
конкретные проблемные звонки, лучше сначала отфильтровать их вручную.
"""

import sys
import time

from utils.logs import setup_logger
from src.database import (
    init_db, delete_processed_calls_range, delete_call_events_range,
    get_success_calls_for_master, get_all_call_events
)
from src.api_client import get_calls_range
from src.call import process_single_call
from src.excel_maker import update_master_report, update_daily_report, update_statistics_report, create_dashboard
from src.sync_manager import YandexFolderSyncer

logger = setup_logger("watchdog")


def reprocess(date_from_day: str, date_till_day: str):
    date_from = f"{date_from_day} 00:00:00"
    date_till = f"{date_till_day} 00:00:00"

    init_db()

    logger.info(f"🔄 [REPROCESS] Старт переобработки диапазона {date_from} — {date_till}")

    deleted_calls = delete_processed_calls_range(date_from, date_till)
    deleted_events = delete_call_events_range(date_from, date_till)
    logger.info(f"🔄 [REPROCESS] Очищено: processed_calls={deleted_calls}, call_events={deleted_events}")

    calls = get_calls_range(date_from, date_till)
    logger.info(f"🔄 [REPROCESS] Получено {len(calls)} записей звонков от Новофона за диапазон")

    processed = skipped = errors = 0
    for i, call in enumerate(calls, start=1):
        result = process_single_call(call)
        if result == "processed":
            processed += 1
        elif result == "skipped":
            skipped += 1
        else:
            errors += 1

        if i % 20 == 0:
            logger.info(f"🔄 [REPROCESS] Прогресс: {i}/{len(calls)} (успешно={processed}, пропущено={skipped}, ошибок={errors})")

        # Небольшая пауза, чтобы не долбить платный fallback слишком агрессивно
        time.sleep(1)

    logger.info(f"🔄 [REPROCESS] Готово: успешно={processed}, пропущено={skipped}, ошибок={errors}")

    # Пересобираем все отчёты и синхронизируем с Диском
    calls_for_report = get_success_calls_for_master()
    update_master_report(calls_for_report)
    update_daily_report(calls_for_report)

    all_events = get_all_call_events()
    update_statistics_report(all_events)
    create_dashboard(all_events, calls_for_report)

    logger.info("🔄 [REPROCESS] Отчёты пересобраны, запускаю синхронизацию с Яндекс.Диском...")
    try:
        syncer = YandexFolderSyncer()
        syncer.sync_reports()
    except Exception as e:
        logger.error(f"❌ [REPROCESS] Ошибка синхронизации: {e}", exc_info=True)

    logger.info("✅ [REPROCESS] Полностью завершено.")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Использование: python3 -m src.reprocess_dates YYYY-MM-DD YYYY-MM-DD")
        print("Пример (переобработать 03, 04, 05 сентября): python3 -m src.reprocess_dates 2026-09-03 2026-09-06")
        sys.exit(1)

    reprocess(sys.argv[1], sys.argv[2])
