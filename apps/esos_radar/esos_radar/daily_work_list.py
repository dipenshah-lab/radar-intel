import pandas as pd

from radar_intel_core.io.csv_utils import write_csv
from .config import GAP_OUTPUT, DAILY_WORK_OUTPUT


def main(n: int = 50, random_state: int = 42) -> None:
    """
    Build a small daily work list from the ESOS gap candidates.

    - Reads GAP_OUTPUT (esos_gap_candidates.csv)
    - Selects key columns for human / automated review
    - Deduplicates by company_number
    - Samples up to n companies at random for today's queue
    - Writes DAILY_WORK_OUTPUT (daily_work_list.csv)
    """
    df = pd.read_csv(GAP_OUTPUT)

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

    write_csv(sample, DAILY_WORK_OUTPUT)
    print(f"Wrote {len(sample)} companies to {DAILY_WORK_OUTPUT}")


if __name__ == "__main__":
    main()
