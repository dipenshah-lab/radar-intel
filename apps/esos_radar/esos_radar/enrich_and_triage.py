from datetime import date
from typing import Any, Dict, List, Optional

import pandas as pd
from requests import RequestException

from radar_intel_core.clients.ch_client import CompaniesHouseClient
from radar_intel_core.io.csv_utils import read_csv, write_csv
from .config import DAILY_WORK_OUTPUT, PROCESSED_DIR

DAILY_INPUT = DAILY_WORK_OUTPUT
ENRICHED_OUTPUT = PROCESSED_DIR / "daily_work_enriched.csv"
ENRICHED_HIGH_OUTPUT = PROCESSED_DIR / "daily_work_enriched_high.csv"

SMALL_TYPES = {"micro-entity", "dormant", "total-exemption-small"}
MID_TYPES = {"total-exemption-full"}
BIG_TYPES = {"full", "group", "large"}  # adjust once you see real values


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

    # Industrial
    if 5 <= code <= 9:
        return "Industrial"
    if 10 <= code <= 33:
        return "Industrial"
    if code == 35:
        return "Industrial"
    if 36 <= code <= 39:
        return "Industrial"

    # Buildings
    if 41 <= code <= 43:
        return "Buildings"
    if code == 68:
        return "Buildings"
    if 81 <= code <= 82:
        return "Buildings"

    # Transport
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


def score_row(row: pd.Series) -> int:
    score = 0

    # Robust country handling (NaN, None, etc.)
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

    # 1) Hard penalty for tiny solo entities
    if accounts_type in SMALL_TYPES and not is_group_like:
        return -2

    # 2) UK country
    if any(
        c in country
        for c in ["ENGLAND", "SCOTLAND", "WALES", "UNITED KINGDOM", "NORTHERN IRELAND"]
    ):
        score += 1

    # 3) Accounts type as size proxy
    if accounts_type in BIG_TYPES:
        score += 2
    elif accounts_type in MID_TYPES:
        score += 1
    elif accounts_type in SMALL_TYPES and is_group_like:
        score += 0

    # 4) Recency / compliance behaviour
    if has_recent_accounts and not accounts_overdue:
        score += 1
    elif accounts_overdue:
        score -= 1

    # 5) Group‑like
    if is_group_like:
        score += 2

    # 6) Sector bump
    if sector in {"Industrial", "Buildings", "Transport"}:
        score += 1

    # 7) Age
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


def main(max_companies: int = 200) -> None:
    client = CompaniesHouseClient()

    df = read_csv(DAILY_INPUT)
    df = df.head(max_companies).copy()

    profiles: List[Dict[str, Any]] = []
    for cn, name in zip(df["company_number"].astype(str), df["company_name"]):
        try:
            profile = client.get_company_profile(cn)
        except RequestException:
            profile = None

        signals = derive_signals(profile)
        sic_raw = (profile or {}).get("sic_codes")
        sic_list = normalise_sic_codes(sic_raw)
        sic_joined = ";".join(sic_list)

        legal_name = (profile or {}).get("company_name") or name or ""
        is_group_like = any(w in legal_name.upper() for w in ["HOLDINGS", "GROUP"])

        entry = {
            "company_number": cn,
            "accounts_type": signals["accounts_type"],
            "last_accounts_made_up_to": signals["last_accounts_made_up_to"],
            "next_accounts_due": signals["next_accounts_due"],
            "has_recent_accounts": signals["has_recent_accounts"],
            "accounts_overdue": signals["accounts_overdue"],
            "sic_codes_ch": sic_joined,
            "is_group_like": is_group_like,
            "date_of_creation": signals["date_of_creation"],
        }
        profiles.append(entry)

    prof_df = pd.DataFrame(profiles)
    df = df.merge(prof_df, on="company_number", how="left")

    df["age_years"] = df["date_of_creation"].apply(company_age_years)
    df["primary_sic"] = df["sic_codes_ch"].str.split(";").str[0]
    df["sector"] = df["primary_sic"].apply(map_sic_to_sector)

    df["esos_score"] = df.apply(score_row, axis=1)
    df.sort_values(["esos_score", "company_number"], ascending=[False, True], inplace=True)

    write_csv(df, ENRICHED_OUTPUT)

    high_df = df[df["esos_score"] >= 2].copy()
    write_csv(high_df, ENRICHED_HIGH_OUTPUT)

    print(f"Wrote {len(df)} enriched companies to {ENRICHED_OUTPUT}")
    print(f"Wrote {len(high_df)} high-priority companies to {ENRICHED_HIGH_OUTPUT}")


if __name__ == "__main__":
    main()
