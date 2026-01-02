Radar Intel architecture
Radar Intel is a small Python monorepo for sustainability “radar” applications that scan public registries for compliance and disclosure risk.
The first app is ESOS Radar, which focuses on UK ESOS late‑filer risk and related signals.

High‑level layout
text
radar-intel/
├─ README.md
├─ pyproject.toml
├─ radar_intel_core/
│  ├─ __init__.py
│  ├─ clients/
│  │  ├─ __init__.py
│  │  └─ ch_client.py
│  └─ io/
│     ├─ __init__.py
│     └─ csv_utils.py
└─ apps/
   └─ esos_radar/
      ├─ README.md
      ├─ requirements.txt
      ├─ data/
      │  └─ processed/
      │     ├─ esos_phase3_action_plans.xlsx
      │     └─ esos_phase3_notifications.xlsx
      └─ esos_radar/
         ├─ __init__.py
         ├─ build_gap_list.py
         ├─ config.py
         ├─ daily_work_list.py
         └─ fetch_companies.py
radar_intel_core/ holds shared libraries and common utilities that can be reused by multiple radar apps.

apps/ contains one folder per radar application; each app is free to evolve at its own pace without polluting the core.

data/ under an app is for app‑specific example or working data, not for long‑term storage.

Core package: radar_intel_core
The core package is the shared toolbox for all radar apps.

Purpose
Provide shared registry clients (e.g. Companies House, tender portals).

Provide shared I/O utilities (CSV/XLSX helpers and similar).

Eventually host common domain models, pipelines and rules infrastructure as more apps appear.

Structure
text
radar_intel_core/
├─ __init__.py
├─ clients/
│  ├─ __init__.py
│  └─ ch_client.py
└─ io/
   ├─ __init__.py
   └─ csv_utils.py
clients/

ch_client.py: shared client for Companies House (or equivalent) used by ESOS Radar and future apps.

io/

csv_utils.py: shared helpers for reading/writing CSVs and working with processed ESOS files.

Importing from apps
Radar apps import core functionality like this:

python
from radar_intel_core.clients.ch_client import CompaniesHouseClient
from radar_intel_core.io.csv_utils import read_gap_list
If a function or class becomes useful in more than one app, it should be moved into radar_intel_core and given a clear home (clients, io, dates, etc.) rather than a giant catch‑all file.

ESOS Radar app: apps/esos_radar
ESOS Radar is the first concrete radar app and currently contains the original ESOS‑sniper logic, lightly reorganised.

Purpose
Read ESOS‑related inputs (e.g. notifications and action plans).

Fetch and enrich company data from registries.

Build worklists and gap lists that highlight ESOS late‑filer risk and other issues.

Structure
text
apps/esos_radar/
├─ README.md
├─ requirements.txt
├─ data/
│  └─ processed/
│     ├─ esos_phase3_action_plans.xlsx
│     └─ esos_phase3_notifications.xlsx
└─ esos_radar/
   ├─ __init__.py
   ├─ build_gap_list.py
   ├─ config.py
   ├─ daily_work_list.py
   └─ fetch_companies.py
README.md describes the ESOS Radar problem, inputs, outputs and how to run it.

requirements.txt contains ESOS‑specific dependencies (if not managed via the root pyproject.toml).

data/processed/ includes ESOS phase 3 example spreadsheets used for development and demos.

esos_radar/ contains the application logic, directly lifted from the original esos-sniper/src:

build_gap_list.py: builds the ESOS gap list from inputs and registry data.

config.py: configuration values and environment handling specific to ESOS Radar.

daily_work_list.py: produces day‑to‑day worklists for follow‑up and triage.

fetch_companies.py: orchestrates calls to registry clients (now via radar_intel_core.clients).

Using shared core code
Where the original ESOS‑sniper project used local modules, ESOS Radar now uses the shared core:

python
# in fetch_companies.py
from radar_intel_core.clients.ch_client import CompaniesHouseClient

# in build_gap_list.py
from radar_intel_core.io.csv_utils import load_processed_notifications
Any ESOS‑specific helper that is unlikely to be reused can stay in the ESOS package; only genuinely shared concerns are promoted to radar_intel_core.

Root of the monorepo
The top of the repository provides an umbrella description and basic packaging.

Root files
README.md describes Radar Intel as a platform and links to apps/esos_radar/README.md for ESOS‑specific details.

pyproject.toml (or requirements.txt) declares dependencies and makes radar_intel_core and apps.esos_radar importable when the project is installed in editable mode.

Optionally a docs/ folder can hold higher‑level product docs and roadmaps.

Typical workflow
Create and activate a virtual environment.

Install the project in editable mode:

bash
pip install -e .
Run ESOS Radar from the app:

bash
# example pattern – adapt to your actual entrypoint
python -m esos_radar.build_gap_list
As additional radar apps (e.g. SDR Radar, CSRD Radar) are added under apps/, they reuse radar_intel_core for shared functionality while keeping regime‑specific logic isolated in their own packages.