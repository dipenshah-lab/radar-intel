# src/build_gap_list.py
from pathlib import Path
import re
import unicodedata

import pandas as pd

from config import RAW_DIR, PROCESSED_DIR
from utils_csv import read_csv, write_csv


CH_INPUT = PROCESSED_DIR / "ch_large_candidates.csv"
ESOS_NOTIFICATIONS_XLSX = RAW_DIR / "esos_phase3_notifications.xlsx"
OUTPUT_PATH = PROCESSED_DIR / "esos_gap_candidates.csv"

# Put the *actual* column name(s) you find in the Phase 3 workbook here.
ESOS_NAME_COLUMNS = [
    "Participant Name",
    "Organisation Name",
]


def normalise_name(name: str) -> str:
    if not isinstance(name, str):
        return ""
    s = name.upper()

    # Strip accents
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))

    # Remove common suffixes
    for suffix in [
        " LIMITED",
        " LTD",
        " LTD.",
        " PLC",
        " PLC.",
        " PUBLIC LIMITED COMPANY",
    ]:
        if s.endswith(suffix):
            s = s[: -len(suffix)]

    # Remove all non-alphanumeric
    s = re.sub(r"[^A-Z0-9]", "", s)

    return s


def load_esos_notifications(path: Path) -> pd.DataFrame:
    # If the workbook has multiple sheets, you may need sheet_name="Phase 3" or similar.
    df = pd.read_excel(path)
    existing_cols = [c for c in ESOS_NAME_COLUMNS if c in df.columns]
    if not existing_cols:
        raise RuntimeError(
            f"None of the expected ESOS name columns found. Got columns: {list(df.columns)}"
        )

    # Take first matching column as the canonical participant name
    name_col = existing_cols[0]
    df = df[[name_col]].copy()
    df.rename(columns={name_col: "participant_name"}, inplace=True)
    df["name_norm"] = df["participant_name"].apply(normalise_name)
    df = df[df["name_norm"] != ""].drop_duplicates("name_norm")
    return df


def main():
    ch_df = read_csv(CH_INPUT)
    if "company_name" not in ch_df.columns:
        raise RuntimeError("company_name column missing from ch_large_candidates.csv")

    ch_df["name_norm"] = ch_df["company_name"].apply(normalise_name)

    esos_df = load_esos_notifications(ESOS_NOTIFICATIONS_XLSX)

    # Anti-join: CH names that do not appear in ESOS notifications
    merged = ch_df.merge(
        esos_df[["name_norm"]],
        on="name_norm",
        how="left",
        indicator=True,
    )

    gap_df = merged[merged["_merge"] == "left_only"].copy()
    gap_df.drop(columns=["_merge"], inplace=True)

    write_csv(gap_df, OUTPUT_PATH)
    print(f"Wrote {len(gap_df)} ESOS gap candidates to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
