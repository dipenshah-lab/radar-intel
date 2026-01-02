# Radar Intel

Radar Intel is a sustainability regtech data-as-a-service prototype that scans public registries to flag late-filer risks and hidden compliance gaps across ESG and climate disclosures.

The first module, **ESOS Radar**, focuses on UK Energy Savings Opportunity Scheme (ESOS) obligations and late-filer risk.

## Structure

- `radar_intel_core/` – shared domain models, registry connectors, pipelines and rules engine used by all radar apps.
- `apps/esos_radar/` – ESOS-specific application built on the core library.
- `docs/` – high-level product documentation and roadmap.
- `infra/` – Docker, CI/CD and infrastructure-as-code.
- `data/` – sample datasets and schemas for development.
- `scripts/` – helper scripts for demos, ETL and maintenance.

## Getting started

```bash
# create and activate a virtualenv of your choice, then:
pip install -e .

# run the ESOS demo scan
python -m esos_radar.cli run-demo
