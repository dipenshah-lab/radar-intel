# ESOS Radar

ESOS Radar is a Python app that identifies UK companies that **should probably care about ESOS Phase 3** but are **not yet visible in the public ESOS notification data**, and then builds a small, prioritised work list for outreach and follow‑up.[web:12][web:4]

It works by:

- Pulling a large candidate set of UK LTD/PLC companies from the Companies House Public Data API.[web:163][web:157]  
- Comparing that list to the ESOS Phase 3 “Responsible Undertaking” workbook published for compliance monitoring.[web:1][web:283]  
- Outputting an “ESOS gap” list and a short daily work list, optionally enriched via the company profile API for fast triage.[web:193][web:155]

---

## Project layout

Within the main `radar-intel` monorepo:

```text
radar-intel/
├─ radar_intel_core/
│  ├─ config.py                # Shared CH config, loads CH_API_KEY
│  ├─ clients/
│  │  └─ ch_client.py          # CompaniesHouseClient (advanced-search wrapper)
│  └─ io/
│     └─ csv_utils.py          # read_csv / write_csv helpers
└─ apps/
   └─ esos_radar/
      ├─ .env                  # CH_API_KEY=...
      ├─ README.md             # ESOS Radar docs
      ├─ data/
      │  ├─ raw/
      │  │  └─ esos_phase3_notifications.xlsx
      │  └─ processed/
      │     ├─ ch_large_candidates.csv
      │     ├─ esos_gap_candidates.csv
      │     ├─ daily_work_list.csv
      │     └─ daily_work_enriched.csv
      └─ esos_radar/
         ├─ __init__.py
         ├─ config.py
         ├─ fetch_companies.py
         ├─ build_gap_list.py
         ├─ daily_work_list.py
         └─ enrich_and_triage.py

## Prerequisites

- **Python**  
  - Version: 3.11+ (project uses a `.venv` in the repo root).

- **Packages** (from `pyproject.toml` / `requirements.txt`):  
  - `pandas`  
  - `openpyxl`  
  - `requests`  
  - `python-dotenv`  
  - `pytest` (dev)

- **Companies House API key**  
  - Create a key via the Companies House developer hub.[web:163][web:157]  
  - Store it in `apps/esos_radar/.env`:

    ```ini
    CH_API_KEY=your_real_companies_house_key_here
    ```

- **ESOS Phase 3 workbook**  
  - Download the latest ESOS Phase 3 “Notification of Compliance” dataset and save as:  
    `apps/esos_radar/data/raw/esos_phase3_notifications.xlsx`.  
  - ESOS Phase 3 defines responsible undertakings and their addresses, which are used as the reference list of notified participants.[web:1][web:281]

---

## Configuration

### Shared core (`radar_intel_core`)

- `radar_intel_core/config.py`  
  - Sets project roots.  
  - Loads `.env` from `apps/esos_radar`.  
  - Defines Companies House API constants:  

    - `CH_API_KEY`  
    - `CH_BASE_URL`, `CH_ADVANCED_SEARCH_URL`, `CH_PAGE_SIZE`, `CH_MAX_RESULTS`.[web:163][web:166]

- `radar_intel_core/clients/ch_client.py`  
  - `CompaniesHouseClient`:

    - `advanced_search_page(...)` – wraps `/advanced-search/companies`.  
    - `advanced_search_all(...)` – paginates until `CH_MAX_RESULTS` or results are exhausted.

### ESOS Radar app config

- `apps/esos_radar/esos_radar/config.py`  
  - Base directory: `apps/esos_radar`.  
  - Paths:

    ```python
    RAW_DIR = data/raw
    PROCESSED_DIR = data/processed
    CH_INPUT = PROCESSED_DIR / "ch_large_candidates.csv"
    ESOS_NOTIFICATIONS_XLSX = RAW_DIR / "esos_phase3_notifications.xlsx"
    GAP_OUTPUT = PROCESSED_DIR / "esos_gap_candidates.csv"
    DAILY_WORK_OUTPUT = PROCESSED_DIR / "daily_work_list.csv"
    ```

---

## End‑to‑end workflow

All commands are run from the repo root (`radar-intel`) with the virtualenv activated.

### 1. Fetch Companies House candidates

```bash
python -m apps.esos_radar.esos_radar.fetch_companies
