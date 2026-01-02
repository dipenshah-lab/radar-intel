# src/config.py
from pathlib import Path
from dotenv import load_dotenv
import os

load_dotenv()

BASE_DIR = Path(__file__).resolve().parents[1]

DATA_DIR = BASE_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"

RAW_DIR.mkdir(parents=True, exist_ok=True)
PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

CH_API_KEY = os.getenv("CH_API_KEY")

if not CH_API_KEY:
    raise RuntimeError("CH_API_KEY not set in .env")

CH_BASE_URL = "https://api.company-information.service.gov.uk"
CH_ADVANCED_SEARCH_URL = f"{CH_BASE_URL}/advanced-search/companies"

# Reasonable defaults
CH_PAGE_SIZE = 5000  # max allowed by advanced search[web:1]
CH_MAX_RESULTS = 20000
