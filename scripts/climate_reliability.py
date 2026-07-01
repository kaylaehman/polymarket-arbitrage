#!/usr/bin/env python3
"""Print the climate calibration/reliability report from the directional store."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.directional.store import DirectionalStore
from core.directional.climate.reliability import climate_reliability, format_report
db = sys.argv[1] if len(sys.argv) > 1 else "data/directional.db"
print(format_report(climate_reliability(DirectionalStore(db))))
