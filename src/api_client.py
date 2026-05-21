from __future__ import annotations

import requests
import time
import json

from datetime import datetime, timedelta

from utils.logs import setup_logger
from tenacity import retry, stop_after_attempt, wait_exponential_jitter, retry_if_exception_type

import config as cfg

logger = setup_logger("analyzer")


def get_record(call_id: str, record_id: str, communication_id: str = None) -> bytes | None:
    if not communication_id:
        communication_id = call_id

    urls_to_try = [
        f"https://app.novofon.ru/system/media/talk/{communication_id}/{record_id}/",
        f"https://my.novofon.ru/system/media/talk/{communication_id}/{record_id}/",
    ]

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://my.novofon.ru/",
        "Accept": "audio/mpeg, audio/wav, */*",
        "Origin": "https://my.novofon.ru"
    }

    for url in urls_to_try:
        try:
            # allow_redirects=True стоит по умолчанию, проверим, куда нас занесет
            r = requests.get(url, headers=headers, timeout=40)
            size_kb = len(r.content) // 1024
            content_type = r.headers.get('Content-Type', '').lower()

            if r.status_code == 200 and size_kb > 15:
                # ЗДЕСЬ КРИТИЧЕСКАЯ ПРОВЕРКА: если пришел HTML вместо звука — это фейк-успех
                if "text/html" in content_type or "application/json" in content_type:
                    logger.debug(f"| [DOWNLOAD] ⚠ Fake success: got {content_type} instead of audio from {url}")
                    continue
                
                logger.info(f"| [DOWNLOAD] ✅ Success: {call_id} ({size_kb} KB, Type: {content_type}) | {url}")
                return r.content
            else:
                logger.debug(f"| [DOWNLOAD] Attempt failed: {url} → {r.status_code} ({size_kb} KB)")
        except Exception as e:
            logger.debug(f"| [DOWNLOAD] Exception on {url}: {e}")

    logger.warning(f"| [DOWNLOAD] ❌ All attempts failed for {call_id}")
    return None

def get_calls(hours_back: int = 5):
    """Получает список звонков с записями"""
    now = datetime.now()
    date_from = (now - timedelta(hours=hours_back)).strftime("%Y-%m-%d %H:%M:%S")
    date_till = (now + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")

    payload = {
        "jsonrpc": "2.0",
        "method": "get.calls_report",
        "id": int(time.time()),
        "params": {
            "access_token": cfg.NOVOFON_TOKEN,
            "date_from": date_from,
            "date_till": date_till,
            "limit": 200,
            "include_ongoing_calls": False
        }
    }

    try:
        resp = requests.post(cfg.NOVOFON_DATAAPI, json=payload, timeout=25)
        resp.raise_for_status()
        
        data = resp.json().get("result", {}).get("data", [])
        
        calls = []
        for call in data:
            logger.debug(f"Call raw data sample: {call}")
            call_records = call.get("call_records") or []
            if not call_records:
                continue

            # Собираем данные об используемых SIP-линиях из ответа API
            ext_sip_id = str(call.get("external_pbx_sip_id") or "").strip()
            ext_sip_name = str(call.get("external_pbx_sip_name") or "").strip()
            # На случай, если в будущем Новофон добавит стандартные поля internal/sip в этот метод:
            internal_val = str(call.get("internal") or "").strip()
            sip_val = str(call.get("sip") or "").strip()

            for rec_id in call_records:
                calls.append({
                    "id": str(call.get("id")),
                    "communication_id": str(call.get("communication_id") or call.get("id")),
                    "record_id": str(rec_id),
                    "start_time": call.get("start_time"),
                    "duration": call.get("talk_duration") or 0,
                    "phone": call.get("contact_phone_number"),
                    "full_record_link": call.get("full_record_file_link"),
                    "internal": internal_val,
                    "sip": sip_val,
                    "ext_sip_id": ext_sip_id,
                    "ext_sip_name": ext_sip_name
                })
        
        logger.info(f"[API] Fetched {len(calls)} records from {len(data)} calls")
        return calls

    except Exception as e:
        logger.error(f"[API] Failed to fetch calls: {e}")
        return []


@retry(
    retry=retry_if_exception_type((requests.exceptions.RequestException, Exception)),
    wait=wait_exponential_jitter(initial=12, max=120),
    stop=stop_after_attempt(15),
    reraise=True
)
@retry(
    retry=retry_if_exception_type((requests.exceptions.RequestException, Exception)),
    wait=wait_exponential_jitter(initial=15, max=180),
    stop=stop_after_attempt(5),
    reraise=True
)
def analyze_audio(audio_bytes: bytes, call_info: dict):
    c_id = call_info.get('id')
    size_kb = len(audio_bytes) / 1024

    logger.info(f"+--- ANALYZE START: {c_id} | {size_kb:.1f} KB")

    try:
        import base64
        audio_b64 = base64.b64encode(audio_bytes).decode('utf-8')

        payload = {
            'call_id': str(c_id),
            'phone': str(call_info.get('phone', '')),
            'duration': str(call_info.get('duration', '')),
            'audio_base64': audio_b64,
            'mime_type': 'audio/mpeg'
        }

        response = requests.post(
            cfg.APPS_SCRIPT_URL,
            json=payload,
            timeout=360   # увеличил
        )

        if response.status_code == 200:
            result_text = response.text.strip()
            logger.info(f"| [APPS_SCRIPT] Response OK for {c_id}")

            # Проверяем, не пришёл ли JSON с ошибкой Gemini
            if "Gemini API error 503" in result_text or "service is currently unavailable" in result_text:
                logger.warning(f"| [GEMINI 503] Сервис перегружен для {c_id}, будет retry")
                raise Exception(f"Gemini 503 Overload: {result_text[:300]}")

            try:
                json.loads(result_text) 
                logger.ai_trace(f"RAW: {result_text}...")
                return result_text
            except:
                logger.warning(f"| [JSON] Некорректный JSON от GAS: {result_text[:300]}")
                return None
        else:
            logger.error(f"| [HTTP {response.status_code}] {response.text[:400]}")
            if response.status_code >= 500:
                raise Exception(f"Server error {response.status_code}")
            return None

    except Exception as e:
        logger.error(f"| [ANALYZER] Error {c_id}: {e}")
        raise 