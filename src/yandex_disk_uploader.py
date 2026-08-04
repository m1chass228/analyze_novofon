# src/yandex_disk_uploader.py
import os
import logging
import yadisk
from yadisk.exceptions import YaDiskError

logger = logging.getLogger("watchdog")

class YandexDiskUploader:
    def __init__(self, token: str, remote_base_path: str = "/Reports"):
        """
        :param token: OAuth-токен Яндекс Диска
        :param remote_base_path: Корневая папка в Диске для отчетов (например, '/Reports')
        """
        self.token = token
        self.remote_base_path = remote_base_path
        self.disk = None
        self._authenticate()

    def _authenticate(self):
        try:
            self.disk = yadisk.YaDisk(token=self.token)
            if self.disk.check_token():
                logger.info("✅ Яндекс Диск: Токен успешно проверен")
                # Создаем базовую директорию, если её нет
                self._mkdir_if_not_exists(self.remote_base_path)
            else:
                logger.error("❌ Яндекс Диск: Неверный или просроченный токен")
                self.disk = None
        except Exception as e:
            logger.error(f"❌ Ошибка инициализации Яндекс Диска: {e}")
            self.disk = None

    def _mkdir_if_not_exists(self, path: str):
        """Создает папку на Диске, если её еще нет"""
        if not self.disk:
            return
        try:
            if not self.disk.exists(path):
                self.disk.mkdir(path)
                logger.debug(f"Создана папка на Диске: {path}")
        except YaDiskError as e:
            logger.error(f"Ошибка при создании папки {path}: {e}")

    def upload_file(self, local_file_path: str, subfolder_name: str = None) -> bool:
        """
        Загружает локальный файл на Яндекс Диск.
        :param local_file_path: Путь к файлу на диске (например, 'reports/master_calls_report.xlsx')
        :param subfolder_name: Имя подпапки (например, 'individual')
        """
        if not self.disk or not os.path.exists(local_file_path):
            logger.error(f"Загрузка невозможна. Файл: {local_file_path}")
            return False

        try:
            file_name = os.path.basename(local_file_path)
            
            # Определяем целевой путь на Яндекс Диске
            target_dir = self.remote_base_path
            if subfolder_name:
                target_dir = f"{self.remote_base_path}/{subfolder_name}"
                self._mkdir_if_not_exists(target_dir)

            remote_file_path = f"{target_dir}/{file_name}"

            # Загружаем файл (overwrite=True перезапишет файл при обновлении Мастера)
            logger.info(f"Загрузка файла на Яндекс Диск: {file_name} -> {remote_file_path}")
            self.disk.upload(local_file_path, remote_file_path, overwrite=True)
            
            logger.info(f"✅ Файл успешно загружен на Яндекс Диск: {file_name}")
            return True

        except YaDiskError as error:
            logger.error(f"❌ Ошибка API Яндекс Диска при загрузке {file_name}: {error}")
            return False
        except Exception as e:
            logger.error(f"❌ Непредвиденная ошибка при загрузке {file_name}: {e}")
            return False