from pathlib import Path
import re
import unicodedata

import pandas as pd

from radar_intel_core.io.csv_utils import read_csv, write_csv
from .config import CH_INPUT, ESOS_NOTIFICATIONS_XLSX, GAP_OUTPUT

# ESOS Phase 3 workbook settings
ESOS_SHEET = "Responsible Undertaking"
ESOS_NAME_COLUMN = "Organisation name"
ESOS_POSTCODE_COLUMN = "Organisation address - Postcode"


def _normalise_postcode(pc: str) -> str:
    """
    Normalise UK postcode and return outward code (area + district),
    e.g. 'BT7 1FZ' -> 'BT7', 'SW1X 7BE' -> 'SW1X'.
    """
    if not isinstance(pc, str):
        return ""
    s = pc.strip().upper()
    # Collapse internal whitespace
    s = re.sub(r"\s+", " ", s)
    # Outward code = everything before last space
    parts = s.split(" ")
    if len(parts) >= 2:
        return parts[0]
    return s


def normalise_name(name: str) -> str:
    """
    Normalise an organisation name for matching.

    Steps:
    - Uppercase
    - Replace '&' with 'AND'
    - Strip diacritics
    - Remove common legal / noise suffixes
    - Remove all non-alphanumeric characters
    """
    if not isinstance(name, str):
        return ""
    s = name.upper()

    # Standardise ampersands before removing punctuation
    s = s.replace("&", " AND ")

    # Remove diacritics and combining marks (accents, etc.)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))

    # Tidy whitespace and commas/dots
    s = re.sub(r"[.,]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()

    # Remove common UK legal suffixes and noise words when they appear at the end
    suffixes = [
        " LIMITED",
        " LTD",
        " LTD.",
        " PLC",
        " PLC.",
        " PUBLIC LIMITED COMPANY",
        " LLP",
        " HOLDINGS",
        " HOLDING",
        " GROUP",
        " TRUST",
        " SERVICES",
        " UK",
        " INTERNATIONAL",
    ]
    # Apply repeatedly in case of stacked suffixes (e.g. 'HOLDINGS LIMITED')
    changed = True
    while changed and s:
        changed = False
        for suffix in suffixes:
            if s.endswith(suffix):
                s = s[: -len(suffix)]
                s = s.rstrip()
                changed = True

    # Keep only alphanumeric characters
    s = re.sub(r"[^A-Z0-9]", "", s)
    return s


def load_esos_notifications(path: Path) -> pd.DataFrame:
    """
    Load ESOS notification workbook from Responsible Undertaking sheet,
    returning names, postcodes, and a combined match key.
    """
    df = pd.read_excel(path, sheet_name=ESOS_SHEET)

    missing = [
        col
        for col in [ESOS_NAME_COLUMN, ESOS_POSTCODE_COLUMN]
        if col not in df.columns
    ]
    if missing:
        raise RuntimeError(
            f"Expected ESOS columns {missing} not found. "
            f"Available columns: {list(df.columns)}"
        )

    df = df[[ESOS_NAME_COLUMN, ESOS_POSTCODE_COLUMN]].copy()
    df.rename(
        columns={
            ESOS_NAME_COLUMN: "participant_name",
            ESOS_POSTCODE_COLUMN: "participant_postcode",
        },
        inplace=True,
    )

    df["name_norm"] = df["participant_name"].apply(normalise_name)
    df["outward_postcode"] = df["participant_postcode"].apply(_normalise_postcode)

    # Combined key: name + outward postcode
    df["match_key"] = df["name_norm"] + "|" + df["outward_postcode"]
    df = df[df["name_norm"] != ""].drop_duplicates("match_key")
    return df


def main() -> None:
    """
    Anti-join: find CH companies NOT in ESOS notifications,
    using combined name+outward_postcode key.
    """
    ch_df = read_csv(CH_INPUT)
    if "company_name" not in ch_df.columns:
        raise RuntimeError("company_name column missing from ch_large_candidates.csv")

    # Normalise CH company name
    ch_df["name_norm"] = ch_df["company_name"].apply(normalise_name)

    # Normalise CH postcode to outward code (if present)
    if "postal_code" in ch_df.columns:
        ch_df["outward_postcode"] = ch_df["postal_code"].apply(_normalise_postcode)
    else:
        ch_df["outward_postcode"] = ""

    ch_df["match_key"] = ch_df["name_norm"] + "|" + ch_df["outward_postcode"]

    # Load ESOS data with the same key
    esos_df = load_esos_notifications(ESOS_NOTIFICATIONS_XLSX)

    merged = ch_df.merge(
        esos_df[["match_key"]],
        on="match_key",
        how="left",
        indicator=True,
    )

    gap_df = merged[merged["_merge"] == "left_only"].copy()
    gap_df.drop(columns=["_merge"], inplace=True)

    write_csv(gap_df, GAP_OUTPUT)
    print(f"Wrote {len(gap_df)} ESOS gap candidates to {GAP_OUTPUT}")


if __name__ == "__main__":
    main()
