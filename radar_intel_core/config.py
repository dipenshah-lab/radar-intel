# radar_intel_core/config.py
from pathlib import Path
import os

from dotenv import load_dotenv

# PROJECT_ROOT = .../radar-intel
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# ESOS app root: .../radar-intel/apps/esos_radar
ESOS_APP_ROOT = PROJECT_ROOT / "apps" / "esos_radar"
ENV_PATH = ESOS_APP_ROOT / ".env"

if ENV_PATH.exists():
    load_dotenv(ENV_PATH)
else:
    # Optional: helps debugging if path is wrong
    print(f"Warning: .env not found at {ENV_PATH}")

CH_API_KEY = os.getenv("CH_API_KEY")
if not CH_API_KEY:
    raise RuntimeError(f"CH_API_KEY not set in .env at {ENV_PATH}")

CH_BASE_URL = "https://api.company-information.service.gov.uk"
CH_ADVANCED_SEARCH_URL = f"{CH_BASE_URL}/advanced-search/companies"
CH_PAGE_SIZE = 5000
CH_MAX_RESULTS = 20000
