import logging
import sys

AI_TRACE_LEVEL = 25
logging.addLevelName(AI_TRACE_LEVEL, "AI_TRACE")

def ai_trace(self, message, *args, **kws):
    if self.isEnabledFor(AI_TRACE_LEVEL):
        self._log(AI_TRACE_LEVEL, message, args, **kws)

logging.Logger.ai_trace = ai_trace

class StrictFormatter(logging.Formatter):
    """Строгий форматтер с использованием ASCII-символов, цветов и времени"""
    
    # ANSI цвета
    GREY = "\x1b[38;20m"
    CYAN = "\x1b[36;20m"
    YELLOW = "\x1b[33;20m"
    RED = "\x1b[31;20m"
    BOLD_RED = "\x1b[31;1m"
    RESET = "\x1b[0m"
    
    # Добавляем %(asctime)s в начало каждой строки, чтобы в файле было видно время
    FORMATS = {
        logging.DEBUG: f"{GREY}%(asctime)s [DEBUG] %(message)s{RESET}",
        logging.INFO: f"%(asctime)s %(message)s",
        AI_TRACE_LEVEL: f"{CYAN}%(asctime)s [AI_TRACE] %(message)s{RESET}",
        logging.WARNING: f"{YELLOW}%(asctime)s [WARN] %(message)s{RESET}",
        logging.ERROR: f"{RED}%(asctime)s [ERROR] %(message)s{RESET}",
        logging.CRITICAL: f"{BOLD_RED}%(asctime)s [CRIT] %(message)s{RESET}"
    }

    def format(self, record):
        # Меняем дефолтный формат вывода времени на более компактный
        log_fmt = self.FORMATS.get(record.levelno, "%(asctime)s %(message)s")
        formatter = logging.Formatter(log_fmt, datefmt="%Y-%m-%d %H:%M:%S")
        return formatter.format(record)

def setup_logger(name=__name__):
    """Инициализация логгера"""
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG) # Корневой уровень — пишем всё

    # Накатываем наш красивый цветной форматтер
    color_formatter = StrictFormatter()

    # Вывод в консоль (пусть остается для тестов или journalctl)
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(color_formatter)
    stdout_handler.setLevel(logging.DEBUG)

    if not logger.handlers:
        logger.addHandler(stdout_handler)
        
    return logger
