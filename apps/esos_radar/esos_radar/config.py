# apps/esos_radar/esos_radar/config.py

from pathlib import Path

# Base is the ESOS app folder (apps/esos_radar)
BASE_DIR = Path(__file__).resolve().parents[1]

DATA_DIR = BASE_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"

# Make sure folders exist
RAW_DIR.mkdir(parents=True, exist_ok=True)
PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

# File paths used by the ESOS scripts
CH_INPUT = PROCESSED_DIR / "ch_large_candidates.csv"
ESOS_NOTIFICATIONS_XLSX = RAW_DIR / "esos_phase3_notifications.xlsx"
GAP_OUTPUT = PROCESSED_DIR / "esos_gap_candidates.csv"
DAILY_WORK_OUTPUT = PROCESSED_DIR / "daily_work_list.csv"
