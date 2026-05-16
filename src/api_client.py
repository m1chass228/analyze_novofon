import requests
import time
import
import json

from datetime import datetime, timedelta
from utils.logs import setup_logger

import config as cfg
logger = setup_logger("analyzer")

def get_record(call_id, record_id):
    url = f"{cfg.NOVOFON_BASE}{call_id}/{record_id}/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://my.novofon.ru/",
        "Accept": "audio/mpeg, audio/wav, */*"
    }

    try:
        r = requests.get(url, headers=headers, timeout=25)
        size_kb = len(r.content) // 1024
        
        if r.status_code == 200 and len(r.content) > 10000:
            logger.debug(f"| [DOWNLOAD] Success: {call_id} ({size_kb} KB)")
            return r.content
        
        logger.warning(f"| [DOWNLOAD] Failed: {call_id} | Code: {r.status_code} | Size: {size_kb} KB")
        return None
    
    except Exception as e:
        logger.error(f"| [DOWNLOAD] Critical error: {e}")
        return None

def get_calls(hours_back=5):
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
            "limit": 100
        }
    }

    try:
        resp = requests.post(cfg.NOVOFON_DATAAPI, json=payload, timeout=20)
        calls = resp.json().get("result", {}).get("data", [])
        return [
            {
                "id": str(call.get("id")),
                "record_id": call.get("call_records")[0],
                "start_time": call.get("start_time"),
                "duration": call.get("talk_duration"),
                "phone": call.get("contact_phone_number")
            }
            for call in calls if call.get("call_records")
        ]
    except Exception as e:
        logger.error(f"[API] Failed to fetch calls: {e}")
        return []

def analyze_audio(audio_bytes, call_info):
    """
    Отправляет аудио в Google Apps Script для анализа через Gemini
    """
    # URL вашего развернутого Web App (замените на свой!)
    APPS_SCRIPT_URL = "https://script.google.com/macros/s/AKfycbxBH62UvhCplNDyX1SWTrC2ettp7r3sZ6rStt_eTcZJjDyYIM_hvcglLfXDeqGubEuGUQ/exec"

    c_id = call_info.get('id')
    size_kb = len(audio_bytes) / 1024

    logger.info(f"+--- ANALYZE START (via Apps Script): {c_id}")
    logger.debug(f"| Size: {size_kb:.1f} KB")

    try:
        # Отправляем байты аудио напрямую в теле POST-запроса
        # Apps Script принимает это через e.postData.getBlob()
        headers = {'Content-Type': 'audio/mpeg'}

        response = requests.post(
            APPS_SCRIPT_URL,
            data=audio_bytes,
            headers=headers,
            timeout=60  # Увеличиваем таймаут, так как Gemini + скрипт могут думать долго
        )

        if response.status_code == 200:
            logger.info("| [APPS_SCRIPT] Processing complete")
            # Проверяем, что ответ — это валидный JSON (результат работы Gemini)
            try:
                result_json = response.text
                logger.ai_trace(f"RAW_RESPONSE:\n{result_json}")
                return result_json
            except Exception as e:
                logger.error(f"| [PARSE ERROR] Failed to parse script response: {e}")
                return None
        else:
            logger.error(f"| [HTTP ERROR] Status: {response.status_code}, Body: {response.text}")
            return None

    except Exception as e:
        logger.error(f"| [FAILURE] Context: {c_id}, Error: {e}")
        return None
