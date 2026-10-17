"""Fake a ZKTeco ADMS device pushing punches to the server (test the full pipeline without hardware).
Usage: python scripts/simulate_device.py [base_url] [pin ...]"""
import sys
import time
from datetime import datetime

import httpx

base = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
pins = sys.argv[2:] or ["1", "2", "3"]
print(httpx.get(f"{base}/iclock/cdata", params={"SN": "SIM0001", "options": "all"}).text)
for pin in pins:
    line = f"{pin}\t{datetime.now():%Y-%m-%d %H:%M:%S}\t0\t1\t0\t0\t0\t0\t0\t0\n"
    r = httpx.post(f"{base}/iclock/cdata", params={"SN": "SIM0001", "table": "ATTLOG", "Stamp": "9999"}, content=line)
    print(pin, r.text)
    time.sleep(1.5)
