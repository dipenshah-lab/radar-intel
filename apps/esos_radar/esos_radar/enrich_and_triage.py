from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import pandas as pd

from radar_intel_core.clients.ch_client import CompaniesHouseClient
from radar_intel_core.config import PROJECT_ROOT


# ---------- Local config for ESOS Radar ----------

# Where enriched and high-priority CSVs will be written.
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"

# ESOS Phase 3 workbook – actual file you confirmed.
ESOS_WORKBOOK_PATH = (
    PROJECT_ROOT
    / "apps"
    / "esos_radar"
    / "data"
    / "raw"
    / "esos_phase3_notifications.xlsx"
)

# Where the ESOS gap list currently lives.
GAP_INPUT = (
    PROJECT_ROOT
    / "apps"
    / "esos_radar"
    / "data"
    / "processed"
    / "esos_gap_candidates.csv"
)


# ---------- Minimal IO and normalisation helpers ----------

def write_csv(df: pd.DataFrame, path: Path | str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)


def read_excel(path: Path | str, sheet_name: str) -> pd.DataFrame:
    return pd.read_excel(path, sheet_name=sheet_name)


def normalise_name(name: str) -> str:
    if not name:
        return ""
    return " ".join(str(name).upper().strip().split())


def _normalise_postcode(postcode: str | None) -> str:
    if not postcode:
        return ""
    pc = str(postcode).upper().strip()
    return pc.split(" ")[0] if pc else ""


# ---------- Scoring and sector logic ----------

SMALL_TYPES = {"micro-entity", "dormant", "total-exemption-small"}
MID_TYPES = {"total-exemption-full"}
BIG_TYPES = {"full", "group", "large"}  # adjust after inspecting real data


def company_age_years(date_of_creation: Optional[str]) -> Optional[float]:
    if not date_of_creation:
        return None
    try:
        d = date.fromisoformat(date_of_creation)
    except ValueError:
        return None
    return (date.today() - d).days / 365.25


def map_sic_to_sector(primary_sic: Optional[str]) -> str:
    if not primary_sic:
        return "Other"
    try:
        code = int(primary_sic[:2])
    except ValueError:
        return "Other"

    if 5 <= code <= 9:
        return "Industrial"
    if 10 <= code <= 33:
        return "Industrial"
    if code == 35:
        return "Industrial"
    if 36 <= code <= 39:
        return "Industrial"

    if 41 <= code <= 43:
        return "Buildings"
    if code == 68:
        return "Buildings"
    if 81 <= code <= 82:
        return "Buildings"

    if 49 <= code <= 53:
        return "Transport"

    return "Other"


def normalise_sic_codes(raw: Any) -> List[str]:
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    return [str(raw).strip()]


def derive_signals(profile: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not profile:
        return {
            "accounts_type": None,
            "last_accounts_made_up_to": None,
            "next_accounts_due": None,
            "has_recent_accounts": False,
            "accounts_overdue": None,
            "date_of_creation": None,
        }

    accounts = profile.get("accounts") or {}
    last_accounts = accounts.get("last_accounts") or {}
    next_accounts = accounts.get("next_accounts") or {}

    last_made_up_to = last_accounts.get("made_up_to")
    accounts_type = last_accounts.get("type")
    next_due = next_accounts.get("due_on")
    overdue = next_accounts.get("overdue")

    has_recent = bool(last_made_up_to)

    return {
        "accounts_type": accounts_type,
        "last_accounts_made_up_to": last_made_up_to,
        "next_accounts_due": next_due,
        "has_recent_accounts": has_recent,
        "accounts_overdue": overdue,
        "date_of_creation": profile.get("date_of_creation"),
    }


def extract_parent_fields(profile: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """
    Best-efforts extraction of parent name/postcode from CH profile.

    Uses foreign_company_details for UK branches of overseas companies. [web:21]
    """
    if not profile:
        return {"parent_name": None, "parent_postcode": None}

    parent_name: Optional[str] = None
    parent_postcode: Optional[str] = None

    fcd = profile.get("foreign_company_details") or {}
    if fcd:
        parent_name = fcd.get("parent_company_name") or parent_name
        parent_addr = fcd.get("parent_company_address") or {}
        parent_postcode = (
            parent_addr.get("postal_code")
            or parent_addr.get("post_code")
            or parent_postcode
        )

    return {
        "parent_name": parent_name,
        "parent_postcode": parent_postcode,
    }


def score_row(row: pd.Series) -> int:
    score = 0

    raw_country = row.get("country")
    country = str(raw_country).upper() if raw_country is not None else ""
    if country == "NAN":
        country = ""

    accounts_type = (row.get("accounts_type") or "").lower()
    sector = row.get("sector") or "Other"
    age = row.get("age_years")

    try:
        age = float(age) if age is not None and not pd.isna(age) else None
    except (TypeError, ValueError):
        age = None

    is_group_like = bool(row.get("is_group_like"))
    has_recent_accounts = bool(row.get("has_recent_accounts"))
    accounts_overdue = bool(row.get("accounts_overdue"))

    # Hard penalty for tiny solo entities
    if accounts_type in SMALL_TYPES and not is_group_like:
        return -2

    # UK country
    if any(
        c in country
        for c in ["ENGLAND", "SCOTLAND", "WALES", "UNITED KINGDOM", "NORTHERN IRELAND"]
    ):
        score += 1

    # Accounts type as size proxy
    if accounts_type in BIG_TYPES:
        score += 2
    elif accounts_type in MID_TYPES:
        score += 1
    elif accounts_type in SMALL_TYPES and is_group_like:
        score += 0

    # Recency / compliance behaviour
    if has_recent_accounts and not accounts_overdue:
        score += 1
    elif accounts_overdue:
        score -= 1

    # Group‑like
    if is_group_like:
        score += 2

    # Sector bump
    if sector in {"Industrial", "Buildings", "Transport"}:
        score += 1

    # Age
    if age is not None:
        if age >= 8:
            score += 1
        elif age < 3 and not is_group_like:
            score -= 1

    if score > 5:
        score = 5
    if score < -2:
        score = -2
    return score


def is_uk_country(val: Optional[str]) -> bool:
    if not val:
        return False
    up = str(val).upper()
    return any(
        c in up for c in ["ENGLAND", "SCOTLAND", "WALES", "UNITED KINGDOM", "NORTHERN IRELAND"]
    )


def load_esos_match_keys(workbook_path: Path | str) -> Set[str]:
    """
    Load ESOS Responsible undertaking sheet and return a set of match_keys
    (normalised name + '|' + outward_postcode) for direct and parent checks.
    Uses 'Organisation name' and 'Organisation address - Postcode' columns. [file:55]
    """
    wb_path = Path(workbook_path)
    df_esos = read_excel(wb_path, sheet_name="Responsible Undertaking")

    name_col = "Organisation name"
    pc_col = "Organisation address - Postcode"

    df_esos["name_norm"] = df_esos[name_col].astype(str).apply(normalise_name)
    df_esos["outward_pc"] = df_esos[pc_col].astype(str).apply(_normalise_postcode)
    df_esos["match_key"] = df_esos["name_norm"] + "|" + df_esos["outward_pc"]

    return set(df_esos["match_key"].dropna().astype(str))


def enrich_and_score(
    gap_csv: Path | str,
    output_full: Path | str,
    output_high: Path | str,
    esos_workbook: Path | str | None = None,
) -> None:
    gap_path = Path(gap_csv)
    df = pd.read_csv(gap_path)

    # TEMP: limit to a random sample while testing to get leads quickly.
    df = df.sample(n=min(300, len(df)), random_state=42)

    client = CompaniesHouseClient()

    esos_keys: Set[str] = set()
    if esos_workbook is not None:
        esos_keys = load_esos_match_keys(esos_workbook)

    accounts_types: List[Optional[str]] = []
    last_made_ups: List[Optional[str]] = []
    next_dues: List[Optional[str]] = []
    has_recent_list: List[bool] = []
    overdue_list: List[Optional[bool]] = []
    creation_dates: List[Optional[str]] = []

    sic_codes_list: List[str] = []
    is_group_like_list: List[bool] = []

    parent_names: List[Optional[str]] = []
    parent_postcodes: List[Optional[str]] = []
    parent_match_keys: List[Optional[str]] = []
    parent_in_esos_flags: List[bool] = []

    print(f"Enriching {len(df)} companies...")

    for i, (_, row) in enumerate(df.iterrows(), start=1):
        if i % 50 == 0:
            print(f"Processed {i}/{len(df)} companies")

        company_number = str(row["company_number"])
        profile = client.get_company_profile(company_number)

        signals = derive_signals(profile)
        accounts_types.append(signals["accounts_type"])
        last_made_ups.append(signals["last_accounts_made_up_to"])
        next_dues.append(signals["next_accounts_due"])
        has_recent_list.append(bool(signals["has_recent_accounts"]))
        overdue_list.append(signals["accounts_overdue"])
        creation_dates.append(signals["date_of_creation"])

        sic_codes_raw = profile.get("sic_codes") or []
        sic_codes_norm = ";".join(normalise_sic_codes(sic_codes_raw))
        sic_codes_list.append(sic_codes_norm)

        name = str(row.get("company_name") or "")
        accounts_type_lower = (signals["accounts_type"] or "").lower()
        group_like = False
        if "group" in accounts_type_lower:
            group_like = True
        elif "holdings" in name.lower() or "group" in name.lower():
            group_like = True
        elif isinstance(sic_codes_raw, list) and len(sic_codes_raw) > 1:
            group_like = True
        is_group_like_list.append(group_like)

        parent_info = extract_parent_fields(profile)
        p_name = parent_info["parent_name"]
        p_pc = parent_info["parent_postcode"]

        if p_name:
            p_name_norm = normalise_name(p_name)
            outward_pc = _normalise_postcode(p_pc) if p_pc else ""
            p_match_key = f"{p_name_norm}|{outward_pc}"
            in_esos = p_match_key in esos_keys if esos_keys else False
        else:
            p_match_key = None
            in_esos = False

        parent_names.append(p_name)
        parent_postcodes.append(p_pc)
        parent_match_keys.append(p_match_key)
        parent_in_esos_flags.append(in_esos)

    df["accounts_type"] = accounts_types
    df["last_accounts_made_up_to"] = last_made_ups
    df["next_accounts_due"] = next_dues
    df["has_recent_accounts"] = has_recent_list
    df["accounts_overdue"] = overdue_list
    df["date_of_creation"] = creation_dates

    df["sic_codes_ch"] = sic_codes_list
    df["is_group_like"] = is_group_like_list

    df["parent_name"] = parent_names
    df["parent_postcode"] = parent_postcodes
    df["parent_match_key"] = parent_match_keys
    df["parent_in_esos"] = parent_in_esos_flags
    df["covered_by_group"] = df["parent_in_esos"]

    df["age_years"] = df["date_of_creation"].apply(company_age_years)
    df["primary_sic"] = df["sic_codes_ch"].str.split(";").str[0]
    df["sector"] = df["primary_sic"].apply(map_sic_to_sector)
    df["esos_score"] = df.apply(score_row, axis=1)

    df.sort_values(
        ["esos_score", "company_number"],
        ascending=[False, True],
        inplace=True,
    )

    write_csv(df, output_full)

    df["accounts_type_lower"] = df["accounts_type"].fillna("").str.lower()
    non_micro_mask = ~df["accounts_type_lower"].isin(["micro-entity", "dormant"])
    score_mask = df["esos_score"] >= 2

    df["is_uk"] = df["country"].apply(is_uk_country)
    uk_mask = df["is_uk"]

    parent_mask = ~df.get(
        "parent_in_esos",
        pd.Series(False, index=df.index),
    ).astype(bool)

    young_solo_mask = ~(
        (df["age_years"] < 3)
        & (~df["is_group_like"])
    )

    high_df = df[
        score_mask
        & non_micro_mask
        & uk_mask
        & parent_mask
        & young_solo_mask
    ].copy()

    write_csv(high_df, output_high)


def main() -> None:
    gap_csv = GAP_INPUT
    full_out = PROCESSED_DIR / "daily_work_enriched.csv"
    high_out = PROCESSED_DIR / "daily_work_enriched_high.csv"
    enrich_and_score(
        gap_csv=gap_csv,
        output_full=full_out,
        output_high=high_out,
        esos_workbook=ESOS_WORKBOOK_PATH,
    )


if __name__ == "__main__":
    main()
