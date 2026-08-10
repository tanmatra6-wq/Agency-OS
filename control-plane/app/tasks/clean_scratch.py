import os
import time
import glob
import logging

logger = logging.getLogger(__name__)

def clean_scratch_db_files(tmp_dir: str = "/tmp", max_age_hours: int = 24):
    """
    Walks the temporary directory and deletes any file matching the pattern
    'scratch_restore_*.db' that is older than max_age_hours.
    """
    pattern = os.path.join(tmp_dir, "scratch_restore_*.db")
    now = time.time()
    max_age_seconds = max_age_hours * 3600
    
    deleted_count = 0
    errors = 0
    
    for filepath in glob.glob(pattern):
        try:
            file_stat = os.stat(filepath)
            # Use mtime
            age_seconds = now - file_stat.st_mtime
            
            if age_seconds > max_age_seconds:
                os.remove(filepath)
                logger.info(f"Deleted residual scratch DB: {filepath} (age: {age_seconds / 3600:.2f} hours)")
                deleted_count += 1
        except Exception as e:
            logger.error(f"Failed to delete {filepath}: {e}")
            errors += 1
            
    return {"status": "ok", "deleted": deleted_count, "errors": errors}

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logger.info("Starting /tmp scratch db cleanup...")
    result = clean_scratch_db_files()
    logger.info(f"Cleanup finished: {result}")
