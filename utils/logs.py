import logging
import sys

AI_TRACE_LEVEL = 25
logging.addLevelName(AI_TRACE_LEVEL, "AI_TRACE")

def ai_trace(self, message, *args, **kws):
    if self.isEnabledFor(AI_TRACE_LEVEL):
        self._log(AI_TRACE_LEVEL, message, args, **kws)

logging.Logger.ai_trace = ai_trace

class StrictFormatter(logging.Formatter):
    """Строгий форматтер с использованием ASCII-символов и цветов"""
    
    # ANSI цвета
    GREY = "\x1b[38;20m"
    CYAN = "\x1b[36;20m"
    YELLOW = "\x1b[33;20m"
    RED = "\x1b[31;20m"
    BOLD_RED = "\x1b[31;1m"
    RESET = "\x1b[0m"
    
    FORMATS = {
        logging.DEBUG: f"{GREY}[DEBUG] %(message)s{RESET}",
        logging.INFO: "%(message)s",
        AI_TRACE_LEVEL: f"{CYAN}[AI_TRACE] %(message)s{RESET}",
        logging.WARNING: f"{YELLOW}[WARN] %(message)s{RESET}",
        logging.ERROR: f"{RED}[ERROR] %(message)s{RESET}",
        logging.CRITICAL: f"{BOLD_RED}[CRIT] %(message)s{RESET}"
    }

    def format(self, record):
        log_fmt = self.FORMATS.get(record.levelno, "%(message)s")
        formatter = logging.Formatter(log_fmt)
        return formatter.format(record)

def setup_logger(name=__name__):
    """Инициализация логгера"""
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    # Вывод в консоль
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(StrictFormatter())
    
    # Запись в файл (app.log)
    file_handler = logging.FileHandler("app.log", encoding="utf-8")
    file_fmt = logging.Formatter('%(asctime)s | %(levelname)s | %(name)s | %(message)s')
    file_handler.setFormatter(file_fmt)
    file_handler.setLevel(logging.INFO)

    if not logger.handlers:
        logger.addHandler(stdout_handler)
        logger.addHandler(file_handler)
        
    return logger