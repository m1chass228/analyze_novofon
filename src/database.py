import sqlite3
import logging
from utils.logs import setup_logger

logger = setup_logger("database")

def init_db():
    conn = sqlite3.connect("calls_analysis.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS processed_calls (
            call_id TEXT PRIMARY KEY,
            start_time TEXT,
            duration INTEGER,
            phone TEXT,
            analysis_text TEXT,
            processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()
    logger.debug("[DB] Initialization complete")

def is_call_processed(call_id):
    conn = sqlite3.connect("calls_analysis.db")
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM processed_calls WHERE call_id = ?", (call_id,))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def save_analysis_to_db(call_id, start_time, duration, phone, analysis_text):
    try:
        conn = sqlite3.connect("calls_analysis.db")
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO processed_calls (call_id, start_time, duration, phone, analysis_text)
            VALUES (?, ?, ?, ?, ?)
        """, (call_id, start_time, duration, phone, analysis_text))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"[DB] Save failed: {e}")

def clear_old_data(days=30):
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
            logger.info(f"[DB] Cleanup: removed {deleted_count} records older than {days} days")
    except Exception as e:
        logger.error(f"[DB] Cleanup failed: {e}")