from __future__ import annotations

import random
from pathlib import Path

import pandas as pd

from radar_intel_core.config import PROJECT_ROOT


PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"
DAILY_WORK_OUTPUT = PROCESSED_DIR / "daily_work_list.csv"
ENRICHED_HIGH_INPUT = PROCESSED_DIR / "daily_work_enriched_high.csv"


def write_csv(df: pd.DataFrame, path: Path | str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)


def main(n: int = 50, random_state: int | None = None) -> None:
    df = pd.read_csv(ENRICHED_HIGH_INPUT)

    cols = [
        "company_number",
        "company_name",
        "address_line_1",
        "address_line_2",
        "locality",
        "postal_code",
        "country",
        "sector",
        "accounts_type",
        "esos_score",
    ]
    existing_cols = [c for c in cols if c in df.columns]
    df_small = df[existing_cols].drop_duplicates("company_number")

    if len(df_small) <= n:
        sample = df_small
    else:
        if random_state is None:
            random_state = random.randint(0, 10_000_000)
        sample = df_small.sample(n=n, random_state=random_state)

    write_csv(sample, DAILY_WORK_OUTPUT)
    print(f"Wrote {len(sample)} companies to {DAILY_WORK_OUTPUT}")


if __name__ == "__main__":
    main()
