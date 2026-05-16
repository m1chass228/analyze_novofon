import sqlite3
from datetime import datetime
from utils.logs import setup_logger

logger = setup_logger("database")


def init_db():
    """Инициализация базы данных"""
    conn = sqlite3.connect("calls_analysis.db")
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS processed_calls (
            call_key            TEXT PRIMARY KEY,     -- составной ключ: call_id_record_id
            call_id             TEXT,
            record_id           TEXT,
            start_time          TEXT,
            duration            INTEGER,
            phone               TEXT,
            analysis_text       TEXT,
            status              TEXT DEFAULT 'success', -- success / download_failed / analysis_failed
            processed_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Индексы для быстрых поисков
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_call_id ON processed_calls(call_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_phone ON processed_calls(phone)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_processed_at ON processed_calls(processed_at)")
    
    conn.commit()
    conn.close()
    logger.info("[DB] Database initialized with composite key support")


def is_call_processed(call_key: str) -> bool:
    """Проверяет, обработан ли уже этот звонок + запись"""
    try:
        conn = sqlite3.connect("calls_analysis.db")
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM processed_calls WHERE call_key = ?", (call_key,))
        result = cursor.fetchone()
        conn.close()
        return result is not None
    except Exception as e:
        logger.error(f"[DB] Check error: {e}")
        return False  # на всякий случай пропускаем, чтобы не зацикливаться


def save_analysis_to_db(call_key: str, start_time, duration, phone, 
                       analysis_text, status: str = "success"):
    """Сохраняет результат анализа"""
    try:
        conn = sqlite3.connect("calls_analysis.db")
        cursor = conn.cursor()
        
        # Разбиваем call_key обратно на части
        call_id = call_key.split('_')[0] if '_' in call_key else call_key
        record_id = call_key.split('_')[1] if '_' in call_key else None

        cursor.execute("""
            INSERT OR REPLACE INTO processed_calls 
            (call_key, call_id, record_id, start_time, duration, phone, analysis_text, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            call_key,
            call_id,
            record_id,
            start_time,
            duration,
            phone,
            analysis_text,
            status
        ))
        
        conn.commit()
        conn.close()
        
        if status == "success":
            logger.debug(f"[DB] Saved analysis for {call_key}")
        else:
            logger.warning(f"[DB] Saved with status '{status}': {call_key}")
            
    except Exception as e:
        logger.error(f"[DB] Save failed for {call_key}: {e}")


def clear_old_data(days: int = 30):
    """Очистка старых записей"""
    try:
        conn = sqlite3.connect("calls_analysis.db")
        cursor = conn.cursor()
        
        cursor.execute(
            "DELETE FROM processed_calls WHERE processed_at < datetime('now', ?)", 
            (f'-{days} days',)
        )
        
        deleted_count = cursor.rowcount
        conn.commit()
        conn.close()
        
        if deleted_count > 0:
            logger.info(f"[DB] Cleanup: removed {deleted_count} old records (> {days} days)")
            
    except Exception as e:
        logger.error(f"[DB] Cleanup failed: {e}")


def get_stats():
    """Полезная функция для отладки/дашборда"""
    try:
        conn = sqlite3.connect("calls_analysis.db")
        cursor = conn.cursor()
        
        cursor.execute("SELECT COUNT(*) FROM processed_calls")
        total = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM processed_calls WHERE status = 'success'")
        success = cursor.fetchone()[0]
        
        conn.close()
        return {"total": total, "success": success}
    except:
        return {"total": 0, "success": 0}