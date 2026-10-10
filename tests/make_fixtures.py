"""Prepare tests/fixtures from Home Assistant's recorded SmartThings washer answers.

The recorded files (devices_*.json, status_da_wm_wm_*.json without a suffix) are Home
Assistant's test fixtures, unchanged except that appliance serial numbers and OTN ids in
"execute" data are replaced with placeholders.

Home Assistant has no paused, finished or error washer, so those are derived from the
recorded running DA_WM_TP1_21_COMMON washer (the platform of the WW5000C series, such as
the WW90CGC04DAH), changing only the attributes named below. Run this again after
updating a recorded file:

  status_da_wm_wm_01011_paused.json            operatingState paused, machineState pause
  status_da_wm_wm_01011_finished.json          operatingState ready, machineState stop,
                                               job state finish, progress 100, 0 min left
  status_da_wm_wm_01011_error.json             the paused washer, plus an error in
                                               samsungce.errorAndAlarmState (shape assumed:
                                               no recorded washer reports one yet)
  status_da_wm_wm_01011_ready_remote_off.json  operatingState ready, machineState stop, job
                                               state none, remote control off

Usage:  python tests/make_fixtures.py
"""
import copy
import json
import re
from pathlib import Path

FIX = Path(__file__).resolve().parent / "fixtures"
REDACT = {"x.com.samsung.da.serialNum": "SERIAL-REDACTED", "x.com.samsung.da.otnDUID": "OTNDUID-REDACTED"}


def redact_recorded():
    for f in sorted(FIX.glob("status_*.json")):
        text = f.read_text(encoding="utf-8")
        new = text
        for key, placeholder in REDACT.items():
            new = re.sub(rf'("{re.escape(key)}"\s*:\s*)"[^"]*"', rf'\1"{placeholder}"', new)
        if new != text:
            f.write_text(new, encoding="utf-8", newline="\n")
            print(f"redacted {f.name}")


def set_value(main, cap, attr, value):
    main[cap][attr]["value"] = value


def derive():
    base = json.loads((FIX / "status_da_wm_wm_01011.json").read_text(encoding="utf-8"))

    def variant(name, change):
        st = copy.deepcopy(base)
        change(st["components"]["main"])
        out = FIX / f"status_da_wm_wm_01011_{name}.json"
        out.write_text(json.dumps(st, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
        print(f"wrote {out.name}")

    def paused(m):
        set_value(m, "samsungce.washerOperatingState", "operatingState", "paused")
        set_value(m, "washerOperatingState", "machineState", "pause")

    def finished(m):
        set_value(m, "samsungce.washerOperatingState", "operatingState", "ready")
        set_value(m, "samsungce.washerOperatingState", "washerJobState", "finish")
        set_value(m, "samsungce.washerOperatingState", "washerJobPhase", "finish")
        set_value(m, "samsungce.washerOperatingState", "progress", 100)
        set_value(m, "samsungce.washerOperatingState", "remainingTime", 0)
        set_value(m, "samsungce.washerOperatingState", "remainingTimeStr", "00:00")
        set_value(m, "washerOperatingState", "machineState", "stop")
        set_value(m, "washerOperatingState", "washerJobState", "finish")

    def error(m):
        paused(m)
        m["samsungce.errorAndAlarmState"] = {"events": {"value": ["4C"], "timestamp": "2025-04-25T09:01:00.000Z"}}

    def ready_remote_off(m):
        set_value(m, "samsungce.washerOperatingState", "operatingState", "ready")
        set_value(m, "samsungce.washerOperatingState", "washerJobState", "none")
        set_value(m, "samsungce.washerOperatingState", "washerJobPhase", "none")
        set_value(m, "samsungce.washerOperatingState", "progress", 1)
        set_value(m, "samsungce.washerOperatingState", "remainingTime", 165)
        set_value(m, "samsungce.washerOperatingState", "remainingTimeStr", "02:45")
        set_value(m, "washerOperatingState", "machineState", "stop")
        set_value(m, "washerOperatingState", "washerJobState", "none")
        set_value(m, "remoteControlStatus", "remoteControlEnabled", "false")

    variant("paused", paused)
    variant("finished", finished)
    variant("error", error)
    variant("ready_remote_off", ready_remote_off)


if __name__ == "__main__":
    redact_recorded()
    derive()
