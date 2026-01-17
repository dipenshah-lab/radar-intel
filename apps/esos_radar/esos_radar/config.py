# apps/esos_radar/esos_radar/config.py
"""
ESOS Radar configuration.

Defines paths for data inputs and outputs.
"""

from pathlib import Path

# Base is the ESOS app folder (apps/esos_radar)
BASE_DIR = Path(__file__).resolve().parents[1]

DATA_DIR = BASE_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"
ARCHIVE_DIR = DATA_DIR / "archive"

# XBRL data directory
XBRL_DIR = RAW_DIR / "xbrl"

# Make sure folders exist
RAW_DIR.mkdir(parents=True, exist_ok=True)
PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
XBRL_DIR.mkdir(parents=True, exist_ok=True)

# ============================================================
# Input files
# ============================================================

# ESOS Phase 3 notifications workbook
ESOS_NOTIFICATIONS_XLSX = RAW_DIR / "esos_phase3_notifications.xlsx"

# ============================================================
# Pipeline outputs (new XBRL-based pipeline)
# ============================================================

# Stage 1: XBRL extraction
XBRL_EXTRACTED = PROCESSED_DIR / "xbrl_extracted.csv"

# Stage 2: ESOS threshold filter
ESOS_QUALIFIED = PROCESSED_DIR / "esos_qualified.csv"

# Stage 3: Gap candidates (not in notifications)
GAP_CANDIDATES = PROCESSED_DIR / "gap_candidates.csv"

# Stage 4: Verified gaps (parent check passed)
VERIFIED_GAPS = PROCESSED_DIR / "verified_gaps.csv"

# Stage 5: Final Tier A+ leads
TIER_A_PLUS_LEADS = PROCESSED_DIR / "tier_a_plus_leads.csv"

# Stage 6: Contact enrichment
ENRICHED_LEADS = PROCESSED_DIR / "enriched_leads.csv"
NEVERBOUNCE_INPUT = PROCESSED_DIR / "neverbounce_input.csv"
FINAL_LEADS = PROCESSED_DIR / "final_leads.csv"

# Daily work list outputs
DAILY_WORK_LIST = PROCESSED_DIR / "daily_work_list.csv"
WORKED_LEADS = PROCESSED_DIR / "worked_leads.csv"

# ============================================================
# Legacy paths (proxy-based pipeline - archived)
# ============================================================

# These are kept for reference but superseded by XBRL pipeline
LEGACY_CH_INPUT = ARCHIVE_DIR / "ch_large_candidates.csv"
LEGACY_GAP_OUTPUT = ARCHIVE_DIR / "esos_gap_candidates.csv"
LEGACY_ENRICHED = ARCHIVE_DIR / "daily_work_enriched.csv"
LEGACY_ENRICHED_HIGH = ARCHIVE_DIR / "daily_work_enriched_high.csv"
