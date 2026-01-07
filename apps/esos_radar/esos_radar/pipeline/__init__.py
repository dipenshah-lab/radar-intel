"""
ESOS-Radar XBRL Pipeline.

Five-stage pipeline for identifying verified ESOS gap leads:

1. stage1_extract_xbrl.py    - Parse XBRL ZIPs, extract financial data
2. stage2_filter_qualified.py - Apply ESOS threshold logic
3. stage3_find_gaps.py       - Remove companies in Phase 3 notifications
4. stage4_check_parents.py   - Trace parents, exclude covered subsidiaries
5. stage5_apply_hygiene.py   - Final cleanup filters

Run with: python -m apps.esos_radar.esos_radar.pipeline.run_pipeline
"""
