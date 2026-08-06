import os
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import yadisk
from yadisk.exceptions import YaDiskError
import config as cfg
# Импортируем функцию обновления мастера, чтобы пересобрать его с реальными ссылками
from src.excel_maker import update_master_report 

logger = logging.getLogger("watchdog")

class YandexFolderSyncer:
    def __init__(self):
        self.token = cfg.YANDEX_TOKEN
        self.local_root = "reports"
        self.remote_root = "app:/reports"
        self.disk = None
        self.max_workers = 8

    def _authenticate(self) -> bool:
        try:
            self.disk = yadisk.YaDisk(token=self.token)
            if self.disk.check_token():
                return True
            logger.error("❌ [SYNC] Неверный токен Яндекс Диска")
            return False
        except Exception as e:
            logger.error(f"❌ [SYNC] Ошибка авторизации: {e}")
            return False

    def _ensure_remote_structure(self):
        # Добавили reports/daily в структуру удаленных папок (statistics теперь плоский файл в корне reports/)
        for path in ["reports", "reports/individual", "reports/daily"]:
            try:
                self.disk.mkdir(f"app:/{path}")
                logger.debug(f"[SYNC] Создана удаленная папка: app:/{path}")
            except yadisk.exceptions.PathExistsError:
                pass
            except Exception as e:
                logger.debug(f"[SYNC] Папка app:/{path} уже существует или недоступна: {e}")

    def _get_remote_files(self) -> set:
        remote_files = set()
        try:
            # 1. Читаем корень удаленной папки (используем правильный .listdir())
            for item in self.disk.listdir(self.remote_root):
                if item.type == "file":
                    remote_files.add(item.name)

            # 2. Читаем ВСЕ файлы из папки individual
            remote_ind_path = f"{self.remote_root}/individual"
            for item in self.disk.listdir(remote_ind_path):
                if item.type == "file":
                    remote_files.add(f"individual/{item.name}")
                    
        except Exception as e:
            logger.warning(f"⚠ [SYNC] Не удалось прочитать удаленные файлы через listdir: {e}")
        return remote_files

    def _delete_remote_file(self, rel_path: str):
        remote_path = f"{self.remote_root}/{rel_path}"
        try:
            self.disk.remove(remote_path)
            logger.debug(f"🗑 [THREAD] Удалён с Диска: {rel_path}")
        except Exception as e:
            logger.warning(f"❌ [THREAD] Не удалось удалить {rel_path}: {e}")

    def _upload_and_get_public_url(self, local_path: str, remote_path: str, rel_path: str) -> str:
        """
        Загружает файл, делает его публичным и возвращает реальную публичную ссылку (https://disk.yandex.ru/i/...)
        """
        lock_delays = [10, 45, 90]
        is_locked_file = False

        for attempt in range(4):
            try:
                # 1. Загружаем файл
                self.disk.upload(local_path, remote_path, overwrite=True)
                
                # 2. Делаем файл ПУБЛИЧНЫМ (shared)
                self.disk.publish(remote_path)
                
                # 3. Запрашиваем метаданные, чтобы вытащить сгенерированную публичную ссылку
                meta = self.disk.get_meta(remote_path)
                public_url = meta.public_url
                
                logger.debug(f"✅ [THREAD] Загружен и опубликован: {rel_path}")
                return public_url # Возвращаем красивую ссылку
                
            except Exception as e:
                err_str = str(e)
                if "DiskResourceLockedError" in err_str or "423" in err_str:
                    is_locked_file = True
                    if attempt < 3:
                        delay = lock_delays[attempt]
                        logger.warning(f"⏳ Файл [{rel_path}] заблокирован Яндексом. Ждем {delay} сек...")
                        time.sleep(delay)
                        continue
                
                if attempt == 3:
                    if is_locked_file:
                        logger.warning(f"⚠ [SKIP] Файл [{rel_path}] занят в веб-версии. Пропускаем.")
                    else:
                        logger.error(f"❌ [THREAD] Ошибка загрузки {rel_path}: {e}")
                    return ""
                else:
                    time.sleep(2)
        return ""

    def sync_reports(self):
        """Сканирует локальные отчеты, загружает новые в облако, сохраняет ссылки в БД и пересобирает сводные журналы"""
        if not self._authenticate():
            return

        self._ensure_remote_structure()

        # 1. Получаем список того, что уже есть на Диске
        remote_files = self._get_remote_files()
        
        # 2. Сканируем локальную папку reports/individual/
        local_individual_dir = os.path.join(self.local_root, "individual")
        if not os.path.exists(local_individual_dir):
            os.makedirs(local_individual_dir, exist_ok=True)

        to_upload_tasks = []
        for root, dirs, files in os.walk(local_individual_dir):
            for file in files:
                if not file.endswith(".xlsx"):
                    continue
                local_path = os.path.join(root, file)
                rel_path = os.path.relpath(local_path, self.local_root)
                
                # Приводим локальный относительный путь к нормальному виду (individual/name.xlsx)
                normalized_rel_path = rel_path.replace(os.sep, "/")
                
                remote_path = f"{self.remote_root}/{normalized_rel_path}"

                # Сравниваем с тем, что реально возвращает _get_remote_files()
                if normalized_rel_path not in remote_files:
                    to_upload_tasks.append((local_path, remote_path, rel_path))

        # 3. Многопоточная загрузка индивидуальных отчетов
        public_urls_map = {}
        if to_upload_tasks:
            logger.info(f"📦 [SYNC] Запуск загрузки {len(to_upload_tasks)} отчетов в {self.max_workers} потоков...")
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                futures = {
                    executor.submit(self._upload_and_get_public_url, l_path, r_path, rel): rel 
                    for l_path, r_path, rel in to_upload_tasks
                }
                for future in as_completed(futures):
                    rel = futures[future]
                    p_url = future.result()
                    if p_url:
                        call_id = os.path.basename(rel).replace("report_", "").replace(".xlsx", "")
                        public_urls_map[call_id] = p_url
                        
                        # === ЖЕСТКИЙ СОХРАН ССЫЛКИ В БД ЧЕРЕЗ СЕТТЕР ===
                        try:
                            from src.database import update_call_report_url_in_db
                            update_call_report_url_in_db(call_id, p_url)
                        except Exception as db_err:
                            logger.error(f"❌ Ошибка сохранения ссылки в БД для {call_id}: {db_err}")
        else:
            logger.info("✨ [SYNC] Нет новых индивидуальных отчетов для загрузки.")

        # Импортируем геттер базы и генератор ежедневного отчета
        from src.database import get_all_calls_from_db_func
        from src.excel_maker import update_daily_report
        from datetime import datetime

        # 4. Достаем данные через свежий геттер
        calls_data = get_all_calls_from_db_func()

        # 5. Пересборка и принудительная заливка Мастер-Отчета
        logger.info("📝 [SYNC] Пересборка Сводного журнала с актуальными публичными ссылками...")
        try:
            master_local_path = update_master_report(calls_data, public_urls_map)
            master_remote_path = f"{self.remote_root}/master_calls_report.xlsx"
            self.disk.upload(master_local_path, master_remote_path, overwrite=True)
            self.disk.publish(master_remote_path)
            master_public_url = self.disk.get_meta(master_remote_path).public_url
            logger.info(f"🚀 [SYNC] Сводный журнал успешно обновлен в облаке! Ссылка: {master_public_url}")
        except Exception as e:
            logger.error(f"❌ Ошибка синхронизации Мастер-отчета: {e}")

        # 6. Пересборка и принудительная заливка Ежедневного отчета за СЕГОДНЯ
        logger.info("📅 [SYNC] Пересборка и загрузка сегодняшнего ежедневного отчета в облако...")
        try:
            daily_local_path = update_daily_report(calls_data, public_urls_map)
            if daily_local_path and os.path.exists(daily_local_path):
                today_filename = os.path.basename(daily_local_path) # Например, 2026-05-26.xlsx
                daily_remote_path = f"{self.remote_root}/daily/{today_filename}"
                
                self.disk.upload(daily_local_path, daily_remote_path, overwrite=True)
                self.disk.publish(daily_remote_path)
                daily_public_url = self.disk.get_meta(daily_remote_path).public_url
                logger.info(f"🚀 [SYNC] Ежедневный отчет {today_filename} обновлен в облаке! Ссылка: {daily_public_url}")
        except Exception as e:
            logger.error(f"❌ Ошибка синхронизации Ежедневного отчета: {e}")

        # 7. Загрузка единого накопительного файла статистики (лежит рядом с мастер-отчётом)
        logger.info("📊 [SYNC] Загрузка файла статистики по внутренним номерам в облако...")
        try:
            stats_local_path = os.path.join(self.local_root, "statistics_report.xlsx")
            if os.path.exists(stats_local_path):
                stats_remote_path = f"{self.remote_root}/statistics_report.xlsx"
                self.disk.upload(stats_local_path, stats_remote_path, overwrite=True)
                self.disk.publish(stats_remote_path)
                stats_public_url = self.disk.get_meta(stats_remote_path).public_url
                logger.info(f"🚀 [SYNC] Статистика обновлена в облаке! Ссылка: {stats_public_url}")
        except Exception as e:
            logger.error(f"❌ Ошибка синхронизации файла статистики: {e}")

        # 8. Загрузка дашборда (графики + ссылки на остальные отчёты)
        logger.info("📋 [SYNC] Загрузка дашборда в облако...")
        try:
            dashboard_local_path = os.path.join(self.local_root, "dashboard.xlsx")
            if os.path.exists(dashboard_local_path):
                dashboard_remote_path = f"{self.remote_root}/dashboard.xlsx"
                self.disk.upload(dashboard_local_path, dashboard_remote_path, overwrite=True)
                self.disk.publish(dashboard_remote_path)
                dashboard_public_url = self.disk.get_meta(dashboard_remote_path).public_url
                logger.info(f"🚀 [SYNC] Дашборд обновлён в облаке! Ссылка: {dashboard_public_url}")
        except Exception as e:
            logger.error(f"❌ Ошибка синхронизации дашборда: {e}")
