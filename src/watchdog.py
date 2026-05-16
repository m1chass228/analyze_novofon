import time 
import logging
from utils.logs import setup_logger
from database import init_db, clear_old_data, is_call_processed, save_analysis_to_db
from api_client import get_calls, get_record, analyze_audio

logger = setup_logger("watchdog")

def watchdog():
    init_db()
    logger.info(">>> Watchdog process started")
    
    last_cleanup_time = 0

    while True:
        current_time = time.time()

        if current_time - last_cleanup_time > 86400:
            clear_old_data(30)
            last_cleanup_time = current_time

        try:
            calls = get_calls(hours_back=3)
            new_found = 0
            
            for call in calls:
                if not is_call_processed(call['id']):
                    new_found += 1
                    logger.info(f"--- Processing new call: {call['id']} ({call['phone']})")
                    
                    audio = get_record(call['id'], call['record_id'])
                    
                    if audio:
                        analysis = analyze_audio(audio, call)
                        
                        if analysis:
                            save_analysis_to_db(
                                call['id'], 
                                call['start_time'], 
                                call['duration'], 
                                call['phone'], 
                                analysis
                            )
                            logger.info(f"[OK] Analysis saved for {call['id']}")
                        
                        time.sleep(10) # гемини лимит
            
            if new_found == 0:
                logger.debug("--- No new calls detected")
                        
        except Exception as e:
            logger.error(f"[CRIT] Watchdog loop error: {e}")
            time.sleep(60)
            
        time.sleep(300)

if __name__ == "__main__":
    watchdog()