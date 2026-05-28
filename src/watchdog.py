import time
import os
import shutil
import logging
import asyncio
from datetime import datetime

# Import database configuration and handlers
from src.database import (
    init_db, clear_old_data,
    get_or_set_period_start, reset_period, get_success_calls_for_master
)
from src.api_client import get_calls
from src.excel_maker import update_master_report, update_daily_report, BASE_REPORTS_DIR
from src.call import process_single_call
from src.sync_manager import YandexFolderSyncer

logger = logging.getLogger("watchdog")

# Runtime settings
PERIOD_DAYS = 30
PERIOD_SECONDS = PERIOD_DAYS * 24 * 3600
SLEEP_BETWEEN_CYCLES = 180  # Delay between polling Novofon API (3 minutes)

# STRICT LIMIT: 1 worker = 1 active stream for LLM requests.
# This completely eliminates race conditions on proxies/GAS accounts.
MAX_CONCURRENT_WORKERS = 1  
SLEEP_BETWEEN_CALLS = 5     # Grace period for the worker between tasks (seconds)

# Asynchronous task queue
call_queue = asyncio.Queue()


async def call_consumer_worker(worker_id: int):
    """
    Dedicated background worker that watches the queue and dispatches
    requests to the LLM strictly one by one.
    """
    logger.info(f"⚙️ [WORKER #{worker_id}] Background consumer pipeline initialized. Listening to queue...")
    while True:
        # Non-blocking extraction of the next call from the queue
        call = await call_queue.get()
        call_id = call.get('id', 'unknown')
        
        try:
            logger.info(f"🚀 [WORKER #{worker_id}] Task assigned. Processing call ID: {call_id}")
            
            # Forward call to the controller. Since there is only ONE worker,
            # proxy selection inside process_single_call will be strictly sequential.
            status = await process_single_call(call)
            
            logger.info(f"✨ [WORKER #{worker_id}] Task finished. Call ID: {call_id} | Status: {status}")
            
            # Non-blocking delay to respect rate-limits and avoid hammering proxies
            await asyncio.sleep(SLEEP_BETWEEN_CALLS)

        except Exception as call_err:
            logger.error(f"💥 [WORKER #{worker_id}] Execution failed for call ID {call_id}: {call_err}", exc_info=True)
        
        finally:
            # Signal the queue that the task has been fully processed
            call_queue.task_done()


async def main_producer_loop():
    """
    Main producer loop: polls Novofon API every 3 minutes 
    and appends new call payloads into the async queue.
    """
    init_db()
    logger.info("🤖 Watchdog service started with single-threaded LLM dispatch mode...")

    is_first_run = True
    CHECK_WINDOW_FIRST_RUN_HOURS = 24
    CHECK_WINDOW_NORMAL_MINUTES = 20

    while True:
        current_time = time.time()

        # 1. 30-day period check
        period_start = get_or_set_period_start()
        if current_time - period_start > PERIOD_SECONDS:
            logger.warning("⏳ [PERIOD] Current 30-day operation window expired. Initiating cleanup...")
            if os.path.exists(BASE_REPORTS_DIR):
                try:
                    shutil.rmtree(BASE_REPORTS_DIR)
                    logger.info("🗑️ Local 'reports' directory successfully purged.")
                except Exception as clean_err:
                    logger.error(f"❌ Failed to delete 'reports' directory: {clean_err}")
            
            reset_period()
            is_first_run = True

        # 2. Evaluate lookup time window
        window = CHECK_WINDOW_FIRST_RUN_HOURS * 60 if is_first_run else CHECK_WINDOW_NORMAL_MINUTES
        logger.info(f"🔍 [PRODUCER] Fetching log events from Novofon API for the last {window} minutes...")

        try:
            # ←←← ИСПРАВИЛ ЗДЕСЬ
            calls = await get_calls()          # ← await обязателен!
            is_first_run = False

            if calls:
                logger.info(f"📦 [PRODUCER] Discovered {len(calls)} call entries. Feeding the queue...")
                
                for call in calls:
                    await call_queue.put(call)

                logger.info(f"⏳ [PRODUCER] Queue loaded. Waiting until all {call_queue.qsize()} tasks are resolved...")
                await call_queue.join()
                logger.info("🎉 [PRODUCER] All call payloads from the current batch successfully processed!")

                # Reports and sync
                logger.info("📊 Rebuilding Excel spreadsheets...")
                calls_for_report = get_success_calls_for_master()
                
                master_path = update_master_report(calls_for_report)
                if master_path:
                    logger.info(f"| [MASTER EXCEL] Updated: {master_path}")
                
                try:
                    daily_path = update_daily_report(calls_for_report)
                    if daily_path:
                        logger.info(f"| [DAILY EXCEL] Updated: {daily_path}")
                except Exception as daily_err:
                    logger.error(f"❌ Failed to generate daily report: {daily_err}")

                try:
                    syncer = YandexFolderSyncer()
                    await syncer.sync_reports()
                except Exception as sync_err:
                    logger.error(f"❌ [SYNC] Failed: {sync_err}")

            else:
                logger.debug("○ [PRODUCER] No new calls this cycle.")

        except Exception as e:
            logger.error(f"💥 [CRITICAL] Main producer cycle crashed: {e}", exc_info=True)
            await asyncio.sleep(60)
            continue

        logger.info(f"💤 [PRODUCER] Cycle finished. Sleeping for {SLEEP_BETWEEN_CYCLES} seconds...")
        await asyncio.sleep(SLEEP_BETWEEN_CYCLES)


async def main():
    """
    Entrypoint coordinator. Provisions the worker pool 
    and runs the infinite producer loop.
    """
    # Spawn a fixed pool containing exactly ONE worker to bypass race conditions
    workers = [
        asyncio.create_task(call_consumer_worker(worker_id=i))
        for i in range(1, MAX_CONCURRENT_WORKERS + 1)
    ]
    
    # Run the main producer pipeline
    await main_producer_loop()


if __name__ == "__main__":
    from utils.logs import setup_logger
    setup_logger("watchdog")
    
    # Fire up the engine inside the native asyncio Event Loop
    asyncio.run(main())