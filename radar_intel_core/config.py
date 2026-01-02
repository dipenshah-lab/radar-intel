# radar_intel_core/config.py
from pathlib import Path
import os

from dotenv import load_dotenv

# Load env from the ESOS app if present
# (adjust if you later have multiple apps with their own envs)
APP_ROOT = Path(__file__).resolve().parents[2] / "apps" / "esos_radar"
ENV_PATH = APP_ROOT / ".env"
if ENV_PATH.exists():
    load_dotenv(ENV_PATH)

BASE_DIR = Path(__file__).resolve().parents[1]

CH_API_KEY = os.getenv("CH_API_KEY")
if not CH_API_KEY:
    raise RuntimeError("CH_API_KEY not set in .env")

CH_BASE_URL = "https://api.company-information.service.gov.uk"
CH_ADVANCED_SEARCH_URL = f"{CH_BASE_URL}/advanced-search/companies"
CH_PAGE_SIZE = 5000
CH_MAX_RESULTS = 20000
