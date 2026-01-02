# src/daily_work_list.py
from pathlib import Path
import pandas as pd

from config import PROCESSED_DIR
from radar_intel_core.io.csv_utils import write_csv


GAP_INPUT = PROCESSED_DIR / "esos_gap_candidates.csv"
OUTPUT_PATH = PROCESSED_DIR / "daily_work_list.csv"


def main(n: int = 20, random_state: int = 42):
    df = pd.read_csv(GAP_INPUT)

    cols = [
        "company_number",
        "company_name",
        "address_line_1",
        "address_line_2",
        "locality",
        "postal_code",
        "country",
    ]
    existing_cols = [c for c in cols if c in df.columns]
    df_small = df[existing_cols].drop_duplicates("company_number")

    if len(df_small) <= n:
        sample = df_small
    else:
        sample = df_small.sample(n=n, random_state=random_state)

    write_csv(sample, OUTPUT_PATH)
    print(f"Wrote {len(sample)} companies to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
