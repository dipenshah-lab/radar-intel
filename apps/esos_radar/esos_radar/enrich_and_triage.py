from typing import Any, Dict, List, Optional

import pandas as pd
from requests import RequestException

from radar_intel_core.clients.ch_client import CompaniesHouseClient
from radar_intel_core.io.csv_utils import read_csv, write_csv
from .config import DAILY_WORK_OUTPUT, PROCESSED_DIR


ENRICHED_OUTPUT = PROCESSED_DIR / "daily_work_enriched.csv"


def fetch_company_profile(
    client: CompaniesHouseClient, company_number: str
) -> Optional[Dict[str, Any]]:
    """
    Call Companies House company profile endpoint for extra signals.

    GET /company/{company_number}
    """
    base_url = "https://api.company-information.service.gov.uk"
    url = f"{base_url}/company/{company_number}"

    resp = client.session.get(url, timeout=30)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def _normalise_sic_codes(raw: Any) -> List[str]:
    """
    Ensure SIC codes are always a list of strings.
    The companyProfile API documents sic_codes as 'array of string',
    but fields can be missing or malformed. [web:193]
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    # If a single string or other type sneaks in, wrap it
    return [str(raw)]


def derive_signals(profile: Dict[str, Any]) -> Dict[str, Any]:
    """
    Derive simple ESOS-relevant signals from a company profile.
    """
    accounts = profile.get("accounts") or {}
    last_accounts = accounts.get("last_accounts") or {}

    last_accounts_date = last_accounts.get("made_up_to")
    next_accounts_due = accounts.get("next_due")
    accounts_type = last_accounts.get("type")

    sic_codes = _normalise_sic_codes(profile.get("sic_codes"))
    company_name = profile.get("company_name") or ""

    is_group_like = any(word in company_name.upper() for word in ["HOLDINGS", "GROUP"])

    return {
        "last_accounts_made_up_to": last_accounts_date,
        "next_accounts_due": next_accounts_due,
        "accounts_type": accounts_type,
        "has_recent_accounts": bool(last_accounts_date),
        "is_group_like": is_group_like,
        "sic_codes_ch": ";".join(sic_codes) if sic_codes else "",
    }


def score_row(row: pd.Series) -> int:
    """
    Simple ESOS relevance score:
    +1 if UK-based
    +1 if has recent accounts
    +1 if 'group-like' (holdings / group etc.)
    """
    score = 0

    raw_country = row.get("country")
    country = str(raw_country).upper() if raw_country is not None else ""
    if any(
        x in country
        for x in ["ENGLAND", "SCOTLAND", "WALES", "UNITED KINGDOM", "NORTHERN IRELAND"]
    ):
        score += 1

    if bool(row.get("has_recent_accounts")):
        score += 1

    if bool(row.get("is_group_like")):
        score += 1

    return score


def main(max_companies: int = 50) -> None:
    """
    Enrich the current daily work list using Companies House
    and compute a simple ESOS score.
    """
    df = read_csv(DAILY_WORK_OUTPUT)

    if "company_number" not in df.columns:
        raise RuntimeError("company_number column missing from daily_work_list.csv")

    df = df.copy().head(max_companies)

    client = CompaniesHouseClient()

    profiles: List[Dict[str, Any]] = []
    for cn in df["company_number"]:
        try:
            profile = fetch_company_profile(client, str(cn))
        except RequestException:
            profile = None
        profiles.append(profile or {})

    prof_df = pd.DataFrame(profiles)

    # Ensure company_number exists in prof_df for the merge
    if not prof_df.empty and "company_number" not in prof_df.columns:
        prof_df["company_number"] = df["company_number"].values

    merged = df.merge(prof_df, on="company_number", how="left", suffixes=("", "_ch"))

    # Derive signals row by row based on the merged data
    signal_rows: List[Dict[str, Any]] = []
    for _, row in merged.iterrows():
        profile_subset = {k: row.get(k) for k in prof_df.columns if k in row}
        signal_rows.append(derive_signals(profile_subset))

    signals = pd.DataFrame(signal_rows)
    for col in signals.columns:
        merged[col] = signals[col]

    merged["esos_score"] = merged.apply(score_row, axis=1)

    merged.sort_values(
        ["esos_score", "company_number"], ascending=[False, True], inplace=True
    )

    write_csv(merged, ENRICHED_OUTPUT)
    print(f"Wrote {len(merged)} enriched companies to {ENRICHED_OUTPUT}")


if __name__ == "__main__":
    main()
