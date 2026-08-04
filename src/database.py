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
    
    # 1. Создаем таблицу звонков
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
            record_url          TEXT,
            report_url          TEXT
        )
    """)

    # 2. создаем таблицу токенов
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS gas_accounts (
            url                 TEXT PRIMARY KEY,
            blocked_until       REAL DEFAULT 0, 
            is_paid             INTEGER DEFAULT 0 
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
    
    if "report_url" not in columns:
        cursor.execute("ALTER TABLE processed_calls ADD COLUMN report_url TEXT")
        logger.info("⚙️ [DB MIGRATION] Добавлена колонка 'report_url' для хранения публичных ссылок")

    # добавляем урлы из конфига 
    import config as cfg
    gas_pool = getattr(cfg, 'GAS_POOL', [])
    for url in gas_pool:
        cursor.execute("INSERT OR IGNORE INTO gas_accounts (url, is_paid) VALUES (?, 0)", (url,))
    # -----------------------------------------------

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS system_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    # 3. Таблица "сырых" событий звонков — пишем ВСЕ звонки (включая потерянные,
    # короткие, без записи), чтобы отдельный файл статистики по внутренним номерам
    # (create_statistics_report) считался корректно, а не только по успешно
    # проанализированным звонкам
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS call_events (
            call_id             TEXT PRIMARY KEY,
            start_time          TEXT,
            direction           TEXT DEFAULT 'in',
            internal_number     TEXT,
            duration            INTEGER DEFAULT 0,
            is_lost             INTEGER DEFAULT 0,
            appointment_made    INTEGER DEFAULT 0
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_call_id ON processed_calls(call_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_phone ON processed_calls(phone)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_processed_at ON processed_calls(processed_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ce_start_time ON call_events(start_time)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ce_internal_number ON call_events(internal_number)")
    
    conn.commit()
    conn.close()
    logger.info("[DB] Database initialized successfully.")


def log_call_event(call_id, start_time, direction, internal_number, duration, is_lost=False):
    """Логирует 'сырое' событие звонка (для статистики по внутренним номерам). Идемпотентно по call_id."""
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR IGNORE INTO call_events
            (call_id, start_time, direction, internal_number, duration, is_lost)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            str(call_id), str(start_time) if start_time else "",
            str(direction) if direction else "in",
            str(internal_number) if internal_number else "",
            int(duration or 0), 1 if is_lost else 0
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"[DB] log_call_event failed for {call_id}: {e}")


def mark_call_appointment(call_id, appointment_made: bool):
    """Помечает событие звонка как завершившееся записью на приём (для метрики 'Запись'/'Конверсия')"""
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE call_events SET appointment_made = ? WHERE call_id = ?",
            (1 if appointment_made else 0, str(call_id))
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"[DB] mark_call_appointment failed for {call_id}: {e}")


def get_call_events_for_date(date_str: str) -> list:
    """Возвращает все 'сырые' события звонков за дату YYYY-MM-DD (включая потерянные/короткие)"""
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT call_id, start_time, direction, internal_number, duration, is_lost, appointment_made
            FROM call_events
            WHERE start_time LIKE ?
        """, (f"{date_str}%",))
        rows = cursor.fetchall()
        conn.close()
        return rows
    except Exception as e:
        logger.error(f"[DB] get_call_events_for_date failed: {e}")
        return []


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
    
# =========================================================================
# GAS ХЕЛПЕРЫ
# =========================================================================

def get_available_gas_url(is_paid: int = 0) -> str | None:
    """Возвращает первый живой (не заблокированный) GAS URL из базы данных (0 - бесплатный, 1 - платный)"""
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        current_time = time.time()
        
        # Передаем параметр is_paid в запрос
        cursor.execute("""
            SELECT url FROM gas_accounts 
            WHERE is_paid = ? AND blocked_until < ? 
            LIMIT 1
        """, (is_paid, current_time))
        
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception as e:
        logger.error(f"[DB] Ошибка при получении живого GAS (is_paid={is_paid}): {e}")
        return None

def block_gas_url(url: str, duration_seconds: int = 60):
    """Помечает GAS-аккаунт как заблокированный на определенное время"""
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        blocked_until = time.time() + duration_seconds
        
        cursor.execute("""
            UPDATE gas_accounts 
            SET blocked_until = ? 
            WHERE url = ?
        """, (blocked_until, url))
        
        conn.commit()
        conn.close()
        logger.warning(f"🔒 [DB LIMIT] Аккаунт {url[-25:]} заблокирован в БД на {duration_seconds} сек.")
    except Exception as e:
        logger.error(f"[DB] Ошибка при блокировке GAS в базе: {e}")

# === CALL ====
def update_call_report_url_in_db(call_id: str, report_url: str):
    """Сеттер: сохраняет постоянную публичную ссылку на индивидуальный отчет для звонка"""
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE processed_calls SET report_url = ? WHERE call_id = ?",
            (report_url, str(call_id))
        )
        conn.commit()
        conn.close()
        logger.debug(f"💾 [DB SETTER] Ссылка на отчет {call_id} успешно сохранена.")
    except Exception as e:
        logger.error(f"❌ [DB SETTER ERROR] Не удалось сохранить ссылку для {call_id}: {e}")


def get_success_calls_for_master() -> list:
    """Геттер для watchdog: возвращает успешные звонки за последние 30 дней для локальной сборки"""
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        # Выбираем строго структурированный список полей
        cursor.execute("""
            SELECT start_time, call_id, duration, phone, admin_name, 
                   clinic_branch, analysis_text, record_url, direction, report_url
            FROM processed_calls
            WHERE status = 'success'
            ORDER BY start_time DESC
        """)
        rows = cursor.fetchall()
        conn.close()
        return rows
    except Exception as e:
        logger.error(f"❌ [DB GETTER ERROR] get_success_calls_for_master: {e}")
        return []


def get_all_calls_from_db_func() -> list:
    """Геттер для sync_manager: возвращает абсолютно все успешные записи звонков из БД"""
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT start_time, call_id, duration, phone, admin_name, 
                   clinic_branch, analysis_text, record_url, direction, report_url
            FROM processed_calls
            WHERE status = 'success'
            ORDER BY start_time DESC
        """)
        rows = cursor.fetchall()
        conn.close()
        return rows
    except Exception as e:
        logger.error(f"❌ [DB GETTER ERROR] get_all_calls_from_db_func: {e}")
        return []
