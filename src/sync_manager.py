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
        for path in ["reports", "reports/individual"]:
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

    def sync_reports(self, get_all_calls_from_db_func=None) -> bool:
        """
        Умная многопоточная синхронизация: загружает ТОЛЬКО новые отчеты,
        удаляет удаленные локально и всегда обновляет Сводный журнал.
        """
        if get_all_calls_from_db_func is None:
            from src.database import get_success_calls_for_master
            get_all_calls_from_db_func = get_success_calls_for_master

        if not os.path.exists(self.local_root):
            return False

        if not self._authenticate():
            return False

        self._ensure_remote_structure()

        # 1. Сканируем локальные файлы индивидуальных отчетов
        local_files_map = {} # rel_path -> local_path
        for root, _, files in os.walk(os.path.join(self.local_root, "individual")):
            for file in files:
                local_path = os.path.join(root, file)
                rel_path = os.path.relpath(local_path, self.local_root).replace("\\", "/")
                local_files_map[rel_path] = local_path

        # 2. Получаем список того, что УЖЕ лежит на Яндекс Диске
        logger.info("🔍 [SYNC] Сканируем файлы в облаке для дельта-анализа...")
        remote_files_set = self._get_remote_files()

        # 3. Вычисляем дельту (что нужно загрузить, а что удалить)
        local_files_set = set(local_files_map.keys())
        
        # То, чего нет в облаке — отправляем на загрузку
        files_to_upload_rel = local_files_set - remote_files_set
        # То, что есть в облаке, но удалено локально — на удаление (кроме мастера)
        files_to_delete_rel = {f for f in (remote_files_set - local_files_set) if not f.startswith("master_calls_report")}

        # 4. Удаляем лишнее в облаке
        if files_to_delete_rel:
            logger.info(f"🗑 [SYNC] Удаляем {len(files_to_delete_rel)} устаревших отчетов с Диска...")
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                executor.map(self._delete_remote_file, files_to_delete_rel)

        # 5. Собираем карту публичных ссылок для ВСЕХ локальных файлов
        # Для старых файлов (которые уже в облаке) — запрашиваем ссылки, для новых — сгенерируем при загрузке
        public_urls_map = {} # call_id -> public_url
        
        # Сначала обрабатываем файлы, которые пропускаем (они уже загружены)
        files_to_skip_rel = local_files_set & remote_files_set
        if files_to_skip_rel:
            logger.info(f"☕ [SYNC] Пропускаем загрузку {len(files_to_skip_rel)} отчетов (уже есть в облаке).")
            
            # Быстро собираем их существующие публичные ссылки, чтобы не сломать Мастер-отчет
            def _fetch_existing_url(rel):
                try:
                    remote_path = f"{self.remote_root}/{rel}"
                    meta = self.disk.get_meta(remote_path)
                    # Если файл почему-то не публичный, публикуем
                    if not meta.public_url:
                        self.disk.publish(remote_path)
                        meta = self.disk.get_meta(remote_path)
                    return rel, meta.public_url
                except Exception as e:
                    logger.debug(f"Не удалось получить линк для {rel}: {e}")
                    return rel, None

            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                results = executor.map(_fetch_existing_url, files_to_skip_rel)
                for rel, p_url in results:
                    if p_url:
                        call_id = os.path.basename(rel).replace("report_", "").replace(".xlsx", "")
                        public_urls_map[call_id] = p_url

        # 6. Загружаем и публикуем ТОЛЬКО новые файлы
        if files_to_upload_rel:
            logger.info(f"📤 [SYNC] Загружаем и публикуем {len(files_to_upload_rel)} новых отчётов...")
            to_upload_tasks = []
            for rel in files_to_upload_rel:
                l_path = local_files_map[rel]
                r_path = f"{self.remote_root}/{rel}"
                to_upload_tasks.append((l_path, r_path, rel))

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
        else:
            logger.info("✨ [SYNC] Нет новых индивидуальных отчетов для загрузки.")

        # 7. Пересборка и принудительная заливка Мастер-Отчета (он обновляется всегда)
        logger.info("📝 [SYNC] Пересборка Сводного журнала с актуальными публичными ссылками...")
        try:
            calls_data = get_all_calls_from_db_func()
            master_local_path = update_master_report(calls_data, public_urls_map)
            
            master_remote_path = f"{self.remote_root}/master_calls_report.xlsx"
            self.disk.upload(master_local_path, master_remote_path, overwrite=True)
            
            self.disk.publish(master_remote_path)
            master_public_url = self.disk.get_meta(master_remote_path).public_url
            logger.info(f"🚀 [SYNC] Сводный журнал успешно обновлен в облаке!")
            logger.info(f"🔗 Ссылка для руководства: {master_public_url}")
        except Exception as e:
            logger.error(f"❌ [SYNC] Ошибка обновления Сводного журнала: {e}")

        return True