# src/fetch_companies.py
import datetime as dt
from pathlib import Path
import pandas as pd

from config import PROCESSED_DIR
from radar_intel_core.clients.ch_client import CompaniesHouseClient
from radar_intel_core.io.csv_utils import write_csv


OUTPUT_PATH = PROCESSED_DIR / "ch_large_candidates.csv"


def main():
    client = CompaniesHouseClient()

    # Older than 5 years on today's date
    cutoff_date = (dt.date.today() - dt.timedelta(days=5 * 365)).isoformat()

    items = client.advanced_search_all(
        company_types=["ltd", "plc"],
        company_statuses=["active"],
        incorporated_to=cutoff_date,
    )

    if not items:
        print("No items returned from advanced search.")
        return

    df = pd.json_normalize(items)

    # Keep a compact but useful subset
    cols = [
        "company_number",
        "company_name",
        "company_status",
        "company_type",
        "date_of_creation",
        "registered_office_address.address_line_1",
        "registered_office_address.address_line_2",
        "registered_office_address.locality",
        "registered_office_address.postal_code",
        "registered_office_address.country",
        "sic_codes",
    ]
    existing_cols = [c for c in cols if c in df.columns]
    df = df[existing_cols].copy()

    df.rename(
        columns={
            "registered_office_address.address_line_1": "address_line_1",
            "registered_office_address.address_line_2": "address_line_2",
            "registered_office_address.locality": "locality",
            "registered_office_address.postal_code": "postal_code",
            "registered_office_address.country": "country",
        },
        inplace=True,
    )

    write_csv(df, OUTPUT_PATH)
    print(f"Wrote {len(df)} companies to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
