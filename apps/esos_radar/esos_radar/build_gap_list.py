# apps/esos_radar/esos_radar/build_gap_list.py
from pathlib import Path
import re
import unicodedata

import pandas as pd

from radar_intel_core.io.csv_utils import read_csv, write_csv
from apps.esos_radar.esos_radar.config import GAP_OUTPUT, CH_INPUT, ESOS_NOTIFICATIONS_XLSX


ESOS_NAME_COLUMNS = [
    "Participant Name",
    "Organisation Name",
]


def normalise_name(name: str) -> str:
    if not isinstance(name, str):
        return ""
    s = name.upper()

    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))

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

    s = re.sub(r"[^A-Z0-9]", "", s)
    return s


def load_esos_notifications(path: Path) -> pd.DataFrame:
    df = pd.read_excel(path)
    existing_cols = [c for c in ESOS_NAME_COLUMNS if c in df.columns]
    if not existing_cols:
        raise RuntimeError(
            f"None of the expected ESOS name columns found. Got columns: {list(df.columns)}"
        )

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

    merged = ch_df.merge(
        esos_df[["name_norm"]],
        on="name_norm",
        how="left",
        indicator=True,
    )

    gap_df = merged[merged["_merge"] == "left_only"].copy()
    gap_df.drop(columns=["_merge"], inplace=True)

    write_csv(gap_df, GAP_OUTPUT)
    print(f"Wrote {len(gap_df)} ESOS gap candidates to {GAP_OUTPUT}")


if __name__ == "__main__":
    main()
