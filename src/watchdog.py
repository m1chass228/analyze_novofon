import time
from utils.logs import setup_logger
from src.database import init_db, clear_old_data, is_call_processed, save_analysis_to_db
from src.api_client import get_calls, get_record, analyze_audio
from src.excel_maker import create_call_report

logger = setup_logger("watchdog")


def generate_call_key(call: dict) -> str:
    """Уникальный ключ для звонка + записи"""
    return f"{call['id']}_{call['record_id']}"


def watchdog():
    init_db()
    logger.info(">>> Watchdog process started. Monitoring Novofon calls...")

    last_cleanup_time = 0
    processed_today = 0

    while True:
        current_time = time.time()

        # Ежедневная очистка старых записей
        if current_time - last_cleanup_time > 86400:  # 24 часа
            clear_old_data(days=30)
            last_cleanup_time = current_time
            processed_today = 0

        try:
            calls = get_calls(hours_back=4)  # чуть увеличил для надёжности
            new_found = 0

            for call in calls:
                call_key = generate_call_key(call)

                if is_call_processed(call_key):  # теперь по составному ключу!
                    continue

                new_found += 1
                logger.info(f"→ New call detected: {call['id']} | {call['phone']} | "
                           f"Duration: {call.get('duration')} сек.")

                # Скачиваем аудио
                audio = get_record(
                    call['id'], 
                    call['record_id'], 
                    call['communication_id']
                )

                if not audio:
                    logger.warning(f"✗ Failed to download audio for {call['id']}")
                    # Всё равно помечаем как обработанный, чтобы не спамить
                    save_analysis_to_db(
                        call_key, call['start_time'], call.get('duration'),
                        call['phone'], None, status="download_failed"
                    )
                    continue

                # Анализируем
                logger.info(f"⚡ Sending to Gemini via GAS: {call['id']}")
                analysis_json = analyze_audio(audio, call)

                if analysis_json:
                    # 1. Сохраняем в базу данных
                    save_analysis_to_db(
                        call_key=call_key,
                        start_time=call['start_time'],
                        duration=call.get('duration'),
                        phone=call['phone'],
                        analysis_text=analysis_json,
                        status="success"  # Упростили, так как мы уже в ветке успеха
                    )
                    processed_today += 1
                    
                    # 2. Генерируем красивый Excel-отчет
                    path_to_excel = create_call_report(
                        gemini_json_str=analysis_json, 
                        call_id=str(call['id']), 
                        duration_sec=int(call.get('duration') or 0)
                    )
                    
                    if path_to_excel:
                        logger.info(f"| [EXCEL] Отчет успешно сохранен: {path_to_excel}")
                    else:
                        logger.warning(f"| [EXCEL] ⚠ Не удалось собрать Excel для {call['id']}")

                    logger.info(f"✓ Successfully analyzed and saved: {call['id']} "
                              f"(Total today: {processed_today})")
                else:
                    logger.error(f"✗ Analysis failed for {call['id']}")

                # Небольшая пауза между звонками (защита от лимитов)
                time.sleep(8)

            if new_found == 0:
                logger.debug("○ No new calls")

        except Exception as e:
            logger.error(f"[CRITICAL] Watchdog loop crashed: {e}", exc_info=True)
            time.sleep(60)

        # Основной sleep
        time.sleep(180)  # проверяем каждые 3 минуты — хороший баланс


if __name__ == "__main__":
    watchdog()