import sqlite3
import time
from datetime import datetime
from utils.logs import setup_logger

logger = setup_logger("database")

DB_NAME = "calls_analysis.db"


def init_db():
    """Инициализация базы данных, создание таблиц и автоматическая миграция старых схем"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    # 1. Создаем таблицу звонков (если её вообще не было)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS processed_calls (
            call_key            TEXT PRIMARY KEY,
            call_id             TEXT,
            record_id           TEXT,
            start_time          TEXT,
            duration            INTEGER,
            phone               TEXT,
            admin_name          TEXT DEFAULT 'Не определен',
            clinic_branch       TEXT DEFAULT 'Не определен',
            direction           TEXT DEFAULT 'in', -- Добавили дефолтное значение
            analysis_text       TEXT,
            status              TEXT DEFAULT 'success',
            processed_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            record_url          TEXT
        )
    """)
    
    # --- БЛОК АВТО-МИГРАЦИИ ДЛЯ СУЩЕСТВУЮЩЕЙ БД ---
    cursor.execute("PRAGMA table_info(processed_calls)")
    columns = [col[1] for col in cursor.fetchall()]
    
    if "admin_name" not in columns:
        try:
            cursor.execute("ALTER TABLE processed_calls ADD COLUMN admin_name TEXT DEFAULT 'Не определен'")
            logger.info("[DB] Миграция: Добавлена колонка admin_name.")
        except Exception as e:
            logger.error(f"[DB] Ошибка добавления колонки admin_name: {e}")
            
    if "clinic_branch" not in columns:
        try:
            cursor.execute("ALTER TABLE processed_calls ADD COLUMN clinic_branch TEXT DEFAULT 'Не определен'")
            logger.info("[DB] Миграция: Добавлена колонка clinic_branch.")
        except Exception as e:
            logger.error(f"[DB] Ошибка добавления колонки clinic_branch: {e}")

    if "direction" not in columns:
        try:
            cursor.execute("ALTER TABLE processed_calls ADD COLUMN direction TEXT DEFAULT 'in'")
            logger.info("[DB] Миграция: Добавлена колонка direction в существующую БД.")
        except Exception as e:
            logger.error(f"[DB] Ошибка добавления колонки direction: {e}")
    # -----------------------------------------------

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS system_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_call_id ON processed_calls(call_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_phone ON processed_calls(phone)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_processed_at ON processed_calls(processed_at)")
    
    conn.commit()
    conn.close()
    logger.info("[DB] Database initialized successfully.")


def is_call_processed(call_key: str) -> bool:
    """Проверяет, обработан ли уже этот звонок"""
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM processed_calls WHERE call_key = ?", (call_key,))
        row = cursor.fetchone()
        conn.close()
        return row is not None
    except Exception as e:
        logger.error(f"[DB] Check failed for {call_key}: {e}")
        return False


def save_analysis_to_db(call_key, call_id, record_id, start_time, duration, phone, analysis_text, status, record_url, admin_name="Не определен", clinic_branch="Не определен", direction="in"):
    """Сохранение результатов с явным указанием admin_name, clinic_branch и direction"""
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO processed_calls 
            (call_key, call_id, record_id, start_time, duration, phone, admin_name, clinic_branch, direction, analysis_text, status, record_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            str(call_key), str(call_id), str(record_id) if record_id else None,
            str(start_time) if start_time else "", int(duration or 0), str(phone) if phone else "",
            str(admin_name) if admin_name else "Не определен", 
            str(clinic_branch) if clinic_branch else "Не определен",
            str(direction) if direction else "in",
            str(analysis_text) if analysis_text else None, str(status), str(record_url) if record_url else None
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"[DB] Save failed for {call_key}: {e}")


def clear_old_data(days: int = 30):
    """Очистка старых записей из лога базы данных"""
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM processed_calls WHERE processed_at < datetime('now', ?)", (f'-{days} days',))
        deleted_count = cursor.rowcount
        conn.commit()
        conn.close()
        if deleted_count > 0:
            logger.info(f"[DB] Cleaned {deleted_count} old rows from database.")
    except Exception as e:
        logger.error(f"[DB] Failed to clear old records: {e}")

# =========================================================================
# ХЕЛПЕРЫ ДЛЯ КОНТРОЛЯ СРОКА ОЧИСТКИ (ФЛАГИ В БД)
# =========================================================================

def get_or_set_period_start() -> float:
    """
    Возвращает timestamp первой записи текущего 30-дневного периода.
    Если флага нет в БД, значит период только начинается — сохраняет текущее время.
    """
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM system_settings WHERE key = 'period_start'")
        row = cursor.fetchone()
        
        if row:
            conn.close()
            return float(row[0])
        
        now_ts = time.time()
        cursor.execute("INSERT INTO system_settings (key, value) VALUES ('period_start', ?)", (str(now_ts),))
        conn.commit()
        conn.close()
        logger.info(f"[DB] Started a new 30-day reporting period starting from now.")
        return now_ts
    except Exception as e:
        logger.error(f"[DB] Error in get_or_set_period_start: {e}")
        return time.time()


def reset_period():
    """Сбрасывает флаг периода из БД (вызывается при уничтожении папки reports)"""
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM system_settings WHERE key = 'period_start'")
        conn.commit()
        conn.close()
        logger.info("[DB] Reporting period flag reset.")
    except Exception as e:
        logger.error(f"[DB] Failed to reset period flag: {e}")


# =========================================================================
# ХЕЛПЕР ДЛЯ ВЫГРУЗКИ ДАННЫХ В МАСТЕР-ОТЧЕТ
# =========================================================================

def get_success_calls_for_master() -> list:
    """Вытаскивает данные для мастера, включая админа, филиал и направление"""
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        # Добавили direction в конец выборки
        cursor.execute("""
            SELECT start_time, call_id, duration, phone, admin_name, clinic_branch, analysis_text, record_url, direction 
            FROM processed_calls 
            WHERE status = 'success'
            ORDER BY start_time DESC
        """)
        rows = cursor.fetchall()
        conn.close()
        return rows
    except Exception as e:
        logger.error(f"[DB] Error in get_success_calls_for_master: {e}")
        return []