import os
import logging
import asyncio
import time
from datetime import datetime
import yadisk
from yadisk.exceptions import YaDiskError
import config as cfg

from src.excel_maker import update_master_report 

logger = logging.getLogger("watchdog")

class YandexFolderSyncer:
    def __init__(self):
        self.token = cfg.YANDEX_TOKEN
        self.local_root = "reports"
        self.remote_root = "app:/reports"
        self.disk = None
        self.max_workers = 8

    async def _authenticate(self, disk: yadisk.AsyncYaDisk) -> bool:
        """Asynchronously authenticates the Yandex Disk token."""
        try:
            if await disk.check_token():
                return True
            logger.error("[SYNC] Invalid Yandex Disk token provided.")
            return False
        except Exception as e:
            logger.error(f"[SYNC] Authentication failed: {e}")
            return False

    async def _ensure_remote_structure(self, disk: yadisk.AsyncYaDisk):
        """Creates target folder layout on remote Yandex Disk if not exists."""
        for path in ["reports", "reports/individual", "reports/daily"]:
            try:
                await disk.mkdir(f"app:/{path}")
                logger.debug(f"[SYNC] Remote folder created successfully: app:/{path}")
            except yadisk.exceptions.PathExistsError:
                pass
            except Exception as e:
                logger.debug(f"[SYNC] Folder app:/{path} already exists or is unavailable: {e}")

    async def _get_remote_files(self, disk: yadisk.AsyncYaDisk) -> set:
        """Asynchronously lists remote files from root and individual directories."""
        remote_files = set()
        try:
            # 1. Read the root folder of the app space
            async for item in disk.listdir(self.remote_root):
                if item.type == "file":
                    remote_files.add(item.name)

            # 2. Read individual audit reports directory
            remote_ind_path = f"{self.remote_root}/individual"
            async for item in disk.listdir(remote_ind_path):
                if item.type == "file":
                    remote_files.add(f"individual/{item.name}")
                    
        except Exception as e:
            logger.warning(f"⚠ [SYNC] Failed to fetch remote file list via listdir: {e}")
        return remote_files

    def _delete_remote_file(self, rel_path: str):
        remote_path = f"{self.remote_root}/{rel_path}"
        try:
            self.disk.remove(remote_path)
            logger.debug(f"🗑 [THREAD] Удалён с Диска: {rel_path}")
        except Exception as e:
            logger.warning(f"❌ [THREAD] Не удалось удалить {rel_path}: {e}")

    async def _upload_and_get_public_url(self, disk: yadisk.AsyncYaDisk, semaphore: asyncio.Semaphore, local_path: str, remote_path: str, rel_path: str) -> str:
        """
        Asynchronously uploads a file, publishes it, and extracts the public shareable link.
        Guarded by a semaphore to prevent API flooding and handles Yandex file lock limits.
        """
        lock_delays = [10, 45, 90]
        is_locked_file = False

        async with semaphore:
            for attempt in range(4):
                try:
                    # 1. Non-blocking file upload
                    await disk.upload(local_path, remote_path, overwrite=True)
                    
                    # 2. Publish (make resource shared public)
                    await disk.publish(remote_path)
                    
                    # 3. Retrieve metadata to extract public link
                    meta = await disk.get_meta(remote_path)
                    public_url = meta.public_url
                    
                    logger.debug(f"✅ [TASK] File uploaded and published successfully: {rel_path}")
                    return public_url
                    
                except Exception as e:
                    err_str = str(e)
                    if "DiskResourceLockedError" in err_str or "423" in err_str:
                        is_locked_file = True
                        if attempt < 3:
                            delay = lock_delays[attempt]
                            logger.warning(f"⏳ File [{rel_path}] is locked by Yandex Web platform. Retrying in {delay}s...")
                            await asyncio.sleep(delay) # Asynchronous sleep releases control loop
                            continue
                    
                    if attempt == 3:
                        if is_locked_file:
                            logger.warning(f"⚠ [SKIP] File [{rel_path}] is busy in web interface. Execution skipped.")
                        else:
                            logger.error(f"❌ [TASK] Failed to upload file {rel_path} after all retries: {e}")
                        return ""
                    else:
                        await asyncio.sleep(2)
            return ""

    async def sync_reports(self):
        """Main async pipeline to scan local storage, upload updates, update DB links, and re-generate spreadsheets."""
        # Initializing the built-in AsyncYaDisk context manager
        async with yadisk.AsyncYaDisk(token=self.token) as disk:
            
            if not await self._authenticate(disk):
                return

            await self._ensure_remote_structure(disk)

            # 1. Fetch existing files from cloud storage
            remote_files = await self._get_remote_files(disk)
            
            # 2. Scanning local individual audit directories
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
                    
                    # Normalize local path separators to forward slashes for Yandex compatibility
                    normalized_rel_path = rel_path.replace(os.sep, "/")
                    remote_path = f"{self.remote_root}/{normalized_rel_path}"

                    if normalized_rel_path not in remote_files:
                        to_upload_tasks.append((local_path, remote_path, rel_path))

            # 3. Concurrent uploading of individual call sheets using asyncio.gather
            public_urls_map = {}
            if to_upload_tasks:
                logger.info(f"📦 [SYNC] Initializing concurrent upload of {len(to_upload_tasks)} reports into Event Loop...")
                
                # Limit simultaneous requests to Yandex servers
                semaphore = asyncio.Semaphore(self.max_concurrent_uploads)
                
                # Creating async tasks for every file needing upload
                tasks = [
                    self._upload_and_get_public_url(disk, semaphore, l_path, r_path, rel)
                    for l_path, r_path, rel in to_upload_tasks
                ]
                
                # Execute all uploads concurrently
                uploaded_urls = await asyncio.gather(*tasks)
                
                # Processing mapping results back to local runtime
                for (l_path, r_path, rel), p_url in zip(to_upload_tasks, uploaded_urls):
                    if p_url:
                        call_id = os.path.basename(rel).replace("report_", "").replace(".xlsx", "")
                        public_urls_map[call_id] = p_url
                        
                        # Save shareable link to application database
                        try:
                            from src.database import update_call_report_url_in_db
                            update_call_report_url_in_db(call_id, p_url)
                        except Exception as db_err:
                            logger.error(f"❌ Database error while storing public link for {call_id}: {db_err}")
            else:
                logger.info("✨ [SYNC] No new individual call sheets found for backup.")

            # Lazy imports for fresh data fetching
            from src.database import get_all_calls_from_db_func
            from src.excel_maker import update_daily_report

            # 4. Extract total unified session lists from DB layer
            calls_data = get_all_calls_from_db_func()

            # 5. Rebuild and overwrite Master Global Spreadsheet
            logger.info("📝 [SYNC] Regenerating Master Call Sheet with fresh cloud links...")
            try:
                master_local_path = update_master_report(calls_data, public_urls_map)
                master_remote_path = f"{self.remote_root}/master_calls_report.xlsx"
                
                # Upload and share Master report
                await disk.upload(master_local_path, master_remote_path, overwrite=True)
                await disk.publish(master_remote_path)
                
                meta = await disk.get_meta(master_remote_path)
                logger.info(f"🚀 [SYNC] Master call journal completely synchronized! Public URL: {meta.public_url}")
            except Exception as e:
                logger.error(f"❌ Error during Master Call Sheet synchronization: {e}")

            # 6. Rebuild and overwrite Daily Current Report Spreadsheet
            logger.info("📅 [SYNC] Re-assembling and uploading today's daily sheet to storage cluster...")
            try:
                daily_local_path = update_daily_report(calls_data, public_urls_map)
                if daily_local_path and os.path.exists(daily_local_path):
                    today_filename = os.path.basename(daily_local_path)
                    daily_remote_path = f"{self.remote_root}/daily/{today_filename}"
                    
                    # Upload and share Daily report
                    await disk.upload(daily_local_path, daily_remote_path, overwrite=True)
                    await disk.publish(daily_remote_path)
                    
                    meta = await disk.get_meta(daily_remote_path)
                    logger.info(f"🚀 [SYNC] Daily sheet {today_filename} synchronized successfully! Public URL: {meta.public_url}")
            except Exception as e:
                logger.error(f"❌ Error during Daily Current Report synchronization: {e}")