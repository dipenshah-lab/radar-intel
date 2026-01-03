from __future__ import annotations

from datetime import date


import pandas as pd

from radar_intel_core.config import PROJECT_ROOT


PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"

HIGH_INPUT = PROCESSED_DIR / "daily_work_enriched_high.csv"
WORKED_INPUT = PROCESSED_DIR / "worked_leads.csv"
DAILY_OUTPUT = PROCESSED_DIR / "daily_work_list.csv"


def main() -> None:
    # Load the high-priority pool
    if not HIGH_INPUT.exists():
        raise RuntimeError(f"High file not found: {HIGH_INPUT}")
    high_df = pd.read_csv(HIGH_INPUT)

    # Ensure company_number is treated as string
    high_df["company_number"] = high_df["company_number"].astype(str)

    # Load or initialise worked leads memory
    if WORKED_INPUT.exists():
        worked_df = pd.read_csv(WORKED_INPUT, dtype={"company_number": str})
    else:
        worked_df = pd.DataFrame(columns=["company_number", "date_first_sent"])

    worked_df["company_number"] = worked_df["company_number"].astype(str)

    # Exclude already worked leads
    merged = high_df.merge(
        worked_df[["company_number"]],
        on="company_number",
        how="left",
        indicator=True,
    )
    fresh = merged[merged["_merge"] == "left_only"].drop(columns=["_merge"])

    if fresh.empty:
        print("No fresh leads available in high file.")
        # Still write an empty daily file for completeness
        fresh.to_csv(DAILY_OUTPUT, index=False)
        return

    # Sort by score (desc) then company_number (asc) just to be explicit
    fresh_sorted = fresh.sort_values(
        ["esos_score", "company_number"],
        ascending=[False, True],
    )

    # Take the top 5 fresh leads
    todays = fresh_sorted.head(5).copy()
    todays.to_csv(DAILY_OUTPUT, index=False)

    # Append todays leads to worked list with date stamp
    todays_worked = todays[["company_number"]].copy()
    todays_worked["date_first_sent"] = date.today().isoformat()

    worked_df = pd.concat([worked_df, todays_worked], ignore_index=True)
    worked_df.drop_duplicates("company_number", inplace=True)
    worked_df.to_csv(WORKED_INPUT, index=False)

    print(f"Wrote {len(todays)} fresh leads to {DAILY_OUTPUT}")
    print(f"Worked leads memory now has {len(worked_df)} rows at {WORKED_INPUT}")


if __name__ == "__main__":
    main()
