import json
import re
import logging
import httpx

from utils.logs import setup_logger
from src.api_client import get_record, analyze_audio
from src.excel_maker import create_call_report
from src.database import is_call_processed, save_analysis_to_db
from utils.is_audio_empty import is_audio_empty

import config as cfg

logger = logging.getLogger("watchdog")

UNKNOWN_VALUE = "Not defined"

def generate_call_key(call: dict) -> str:
    return f"{call['id']}_{call['record_id']}"

def clean_to_10_digits(phone_number) -> str:
    """Cleans the phone number from any characters and returns the last 10 digits for precise matching."""
    if not phone_number:
        return ""
    digits = re.sub(r'\D', '', str(phone_number))
    return digits[-10:] if len(digits) >= 10 else digits

async def process_single_call(call: dict) -> str:
    """
    Asynchronously processes a single call session.
    Returns status: 'processed', 'skipped', or 'error'
    """
    try:
        call_id = str(call.get('id'))
        logger.debug(f"[CALL] Checking call session validation for ID: {call_id}")

        # =====================================================================
        # 🚫 1. FILTER: VIRTUAL PHONE NUMBER (FROM CONFIG)
        # =====================================================================
        virtual_phone = str(call.get('virtual_phone_number') or call.get('did') or call.get('destination') or '').strip()
        cleaned_virtual = re.sub(r'\D', '', virtual_phone)

        blocked_numbers = getattr(cfg, 'BLOCK_NUMBERS', [])
        
        for b_num in blocked_numbers:
            b_num_clean = re.sub(r'\D', '', str(b_num))
            if b_num_clean and (b_num_clean in cleaned_virtual or cleaned_virtual.endswith(b_num_clean)):
                logger.info(f"🚫 [BLACKLIST] Call {call_id} skipped: virtual phone {virtual_phone} is blacklisted in config.")
                return "skipped"

        # =====================================================================
        # 🚫 2. FILTER: ADMINISTRATOR / EMPLOYEE NAME (FROM CONFIG)
        # =====================================================================
        employees = call.get('employees', [])
        emp_name = employees[0].get('employee_full_name', '') if employees else ''
        
        novofon_admin = str(
            call.get('admin_name') or 
            call.get('first_answered_employee_full_name') or 
            call.get('last_answered_employee_full_name') or 
            emp_name or ''
        ).strip().lower()

        blocked_admins = getattr(cfg, 'BLOCK_ADMINS', [])
        
        if any(b_admin.strip().lower() in novofon_admin for b_admin in blocked_admins if b_admin):
            logger.info(f"🚫 [BLACKLIST] Call {call_id} skipped: administrator '{novofon_admin}' is blacklisted in config.")
            return "skipped"

        # Parsing record targets
        record_id = call.get('record_id')
        if isinstance(record_id, dict) and 'id' in record_id:
            record_id = record_id.get('id')

        talk_duration = int(call.get('talk_duration') or call.get('duration') or 0)
        direction = call.get('direction', 'in')

        if not record_id:
            logger.debug(f"○ [CALL] Call {call_id} skipped: record_id is missing (no audio recording found)")
            return "skipped"

        if talk_duration < getattr(cfg, 'MIN_CALL_DURATION_SEC', 25):
            logger.debug(f"○ [CALL] Call {call_id} skipped: duration {talk_duration}s is below the minimum limit.")
            return "skipped"

        call_key = generate_call_key(call)
        if is_call_processed(call_key):
            logger.debug(f"○ [CALL] Call {call_id} skipped: already processed previously (key: {call_key})")
            return "skipped"

        admin_name = call.get('admin_name') or UNKNOWN_VALUE
        customer_phone = call.get('phone') or UNKNOWN_VALUE

        logger.info(f"→ [CALL] New call verified for analysis: {call_id} | {customer_phone} | {talk_duration}s | Direction: {direction}")

        # === Download call recording audio ===
        # Non-blocking call to get_record (which inside uses httpx.AsyncClient)
        audio = await get_record(call_id, record_id, call.get('communication_id'))
        if not audio:
            logger.warning(f"❌ [CALL] Failed to download audio stream for call ID: {call_id}")
            return "error"
        
        # === Check audio for silence / dial tones ===
        if is_audio_empty(audio):
            logger.info(f"⏭️ [CALL] Call {call_id} skipped: audio recording contains only dial tones or technical silence.")
            
            # Save to DB with skipped status to avoid requesting it again in future loops
            save_analysis_to_db(
                call_key=call_key, call_id=call_id, record_id=record_id,
                start_time=call.get('start_time'), duration=talk_duration,
                phone=customer_phone, analysis_text="{}",
                status="skipped", record_url="",
                admin_name=admin_name, clinic_branch=UNKNOWN_VALUE,
                direction=direction
            )
            return "skipped"

        # === Send audio payload to the LLM Dispatcher ===
        logger.info(f"⚡ [CALL] Initializing LLM audit (attempt 1/3) for call ID: {call_id}")
        
        # Await the async analysis (handles GAS pool switches and OpenRouter fallbacks)
        raw_response = await analyze_audio(audio, call)

        if not raw_response:
            logger.error(f"❌ [CALL] All analysis attempts failed for call {call_id} (received None response)")
            return "error"

        # === PARSING & CUSTOMIZING RESPONSE FIELDS ===
        clinic_branch = UNKNOWN_VALUE
        final_admin_display = admin_name
        analysis_json = raw_response

        try:
            parsed = json.loads(raw_response)
            
            clinic_branch = parsed.get("clinic_branch")
            if not clinic_branch or clinic_branch == "null": 
                clinic_branch = UNKNOWN_VALUE

            # === Merging/Formatting administrator name display ===
            novofon_tube = admin_name.strip() if admin_name else ""
            if novofon_tube in ["Не определен", "UNKNOWN_VALUE", "null", "None", "Not defined"]:
                novofon_tube = ""

            gemini_admin = parsed.get("admin_name", "").strip()
            if gemini_admin in ["Не представился(-ась)", "Не определен", "null", "None", "Not represented"]:
                gemini_admin = ""

            # Standard format combination rule:
            if novofon_tube and gemini_admin:
                final_admin_display = f"{novofon_tube} ({gemini_admin})"
            elif novofon_tube:
                final_admin_display = novofon_tube
            elif gemini_admin:
                final_admin_display = gemini_admin
            else:
                final_admin_display = UNKNOWN_VALUE

            # Overwriting field internally so excel_maker receives formatted string
            parsed["admin_name"] = final_admin_display
            analysis_json = json.dumps(parsed, ensure_ascii=False)

        except Exception as parse_err:
            logger.warning(f"⚠ [CALL] Failed to customize specific JSON fields: {parse_err}")

        # Save successful pipeline evaluation to application database
        record_url = f"https://app.novofon.ru/system/media/talk/{call.get('communication_id', call_id)}/{record_id}/"
        save_analysis_to_db(
            call_key=call_key, call_id=call_id, record_id=record_id,
            start_time=call.get('start_time'), duration=talk_duration,
            phone=customer_phone, analysis_text=analysis_json,
            status="success", record_url=record_url,
            admin_name=final_admin_display, clinic_branch=clinic_branch,
            direction=direction
        )

        # Generate individual call spreadsheet (can run synchronously as it writes to local file system)
        path_to_excel = create_call_report(
            gemini_json_str=analysis_json,
            call_id=call_id,
            duration_sec=talk_duration,
            record_url=record_url,
            customer_phone=customer_phone,
            call_start_time=call.get('start_time'),
            direction=direction
        )

        if path_to_excel:
            logger.info(f"| [EXCEL] Individual call spreadsheet created successfully: {path_to_excel}")

        logger.info(f"✅ [CALL] Session fully processed: {call_id} | Operator: {final_admin_display} | Direction: {direction}")
        return "processed"

    except Exception as e:
        logger.error(f"❌ [CALL] Critical exception occurred while processing call session {call.get('id', 'unknown')}: {e}", exc_info=True)
        return "error"