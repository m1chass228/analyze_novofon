import sys
import logging
import asyncio

from utils.logs import setup_logger
from src.watchdog import main_producer_loop, call_consumer_worker
import config as cfg

# Инициализируем главный логгер для точки входа
logger = setup_logger("main")

async def main():
    """
    Entry point for the Novofon Call Analysis System-.
    """
    logger.info("========================================")
    logger.info("  NOVOFON AI ANALYZER SERVICE STARTING  ")
    logger.info("========================================")
    
    try:
        # Spawn fixed pool of decoupled parallel consumers
        workers = [
            asyncio.create_task(call_consumer_worker(worker_id=i))
            for i in range(1, cfg.MAX_CONCURRENT_WORKERS + 1)
        ]
        
        # Run the main producer pipeline
        await main_producer_loop()
    except KeyboardInterrupt:
        logger.info("----------------------------------------")
        logger.info("STOPPED BY USER (SIGINT)")
        logger.info("----------------------------------------")
        sys.exit(0)
    except Exception as e:
        logger.critical(f"FATAL ERROR DURING STARTUP: {e}")
        sys.exit(1)

if __name__ == "__main__":
    from utils.logs import setup_logger
    # Assuming logger wrapper initialization sets defaults
    setup_logger("watchdog")
    
    # Fire up the engine inside the native asyncio Event Loop
    asyncio.run(main())
