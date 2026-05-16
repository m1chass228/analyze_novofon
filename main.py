import sys
import logging
from utils.logs import setup_logger
from watchdog import watchdog

# Инициализируем главный логгер для точки входа
logger = setup_logger("main")

def main():
    """
    Entry point for the Novofon Call Analysis System.
    """
    logger.info("========================================")
    logger.info("  NOVOFON AI ANALYZER SERVICE STARTING  ")
    logger.info("========================================")
    
    try:
        # Запуск бесконечного цикла мониторинга
        watchdog()
    except KeyboardInterrupt:
        logger.info("----------------------------------------")
        logger.info("STOPPED BY USER (SIGINT)")
        logger.info("----------------------------------------")
        sys.exit(0)
    except Exception as e:
        logger.critical(f"FATAL ERROR DURING STARTUP: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()