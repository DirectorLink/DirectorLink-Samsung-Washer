"""LIVE tests: run the real driver.lua against the real SmartThings API and a real washer.

  set ST_TOKEN=<personal access token>          (see README: required scopes)

  python tests/live_test.py status [--save]      READ ONLY. Discover the washers, read the selected one
                                                 and print what the driver makes of it: properties, the
                                                 eleven variables and Print Diagnostics. --save writes
                                                 the /status answer to tests/fixtures/status_live_<model>.json
                                                 with serial numbers, ids and MAC addresses replaced
                                                 (check it before committing).
  python tests/live_test.py watch                READ ONLY. Read every 30 s and print every state change
                                                 and event until Ctrl+C: start a cycle at the washer and
                                                 watch Cycle Started ... Cycle Finished.
  python tests/live_test.py pause-resume         OPERATES THE WASHER. Needs a running cycle with Smart
                                                 Control on: pauses it, waits for the washer, resumes it.
  python tests/live_test.py create-app           create the OAuth app via the driver (saved to --state)
  python tests/live_test.py oauth <redirect-url> exchange the code, renew the token once, read status

The driver runs inside the mocked Control4 runtime (tests/c4_mock.lua); HTTP goes
to the real api.smartthings.com. Timers advance in real time.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lupa import luajit21  # noqa: E402

from test_driver import VARIABLES, Driver, lua_to_py  # noqa: E402

API = "https://api.smartthings.com/v1"
FIX = Path(__file__).resolve().parent / "fixtures"
PROPS = ("Driver Status", "Model", "Reported Features", "Connection", "State", "Job", "Programme", "Time Remaining",
         "Finish Time", "Progress", "Spin", "Temperature", "Rinses", "Kids Lock", "Remote Control", "Delay End",
         "Power", "Energy", "Last Finished")


class LiveCloud:
    """Same interface as FakeSmartThings.handle, but real HTTPS."""

    def __init__(self):
        self.last_status = None

    def handle(self, method, url, headers, data):
        t0 = time.time()
        req = urllib.request.Request(url, data=data.encode() if data else None, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                code, body = r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            code, body = e.code, e.read().decode()
        except Exception as e:  # noqa: BLE001
            print(f"   !! {method} {url} -> {e}")
            return None, None
        short = "/oauth/token" if "oauth/token" in url else url.replace(API, "")
        print(f"   {method:4} {short[:72]:72} -> {code} ({int((time.time() - t0) * 1000)} ms)")
        if url.endswith("/status") and code == 200:
            self.last_status = body
        return code, body


def wait_for(d, predicate, timeout=70):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if predicate():
            return time.time() - t0
        time.sleep(1)
        d.advance(1000)
    return None


class Results:
    def __init__(self):
        self.items = []

    def check(self, name, ok, detail=""):
        self.items.append((name, bool(ok)))
        print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""))

    def summary(self):
        passed = sum(ok for _, ok in self.items)
        print(f"\n{passed}/{len(self.items)} live checks passed")
        return 0 if passed == len(self.items) else 1


def scrub(text):
    """Replace serial numbers, unique ids and MAC addresses in a /status answer."""
    for key in ("x.com.samsung.da.serialNum", "x.com.samsung.da.otnDUID", "serialNumber", "serialNumberExtra",
                "x.com.samsung.da.macAddress", "macAddress", "bleMac", "wifiMac"):
        text = re.sub(rf'("{re.escape(key)}"\s*:\s*)"[^"]*"', rf'\1"REDACTED"', text)
        text = re.sub(rf'("{re.escape(key)}"\s*:\s*\{{\s*"value"\s*:\s*)"[^"]*"', rf'\1"REDACTED"', text)
    return re.sub(r"\b([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b", "00:00:00:00:00:00", text)


def connect(token, device):
    cloud = LiveCloud()
    d = Driver(luajit21, cloud)
    d.init()
    print("\n== Discovery and status (Personal Access Token mode)")
    d.set_property("Personal Access Token", token)
    d.action("DiscoverDevices")
    print("   washers found:", d.M.plist["Select Washer"])
    if device:
        d.set_property("Device ID", device)
    return d, cloud


def show(d):
    for p in PROPS:
        print(f"   {p:18}: {d.prop(p)}")
    print("   variables:", ", ".join(f"{n}={d.var(n)!r}" for n in VARIABLES))


def run_status(token, device, save):
    R = Results()
    d, cloud = connect(token, device)
    dev = d.prop("Device ID")
    R.check("washer discovered and selected", dev, dev[:8] if dev else "none - pass --device <deviceId>")
    if not dev:
        return R.summary()
    show(d)
    R.check("status read", d.prop("Driver Status") in ("OK",) or d.var("STATE") != "", d.prop("Driver Status"))
    R.check("STATE is one of the six", d.var("STATE") in ("Off", "Ready", "Running", "Paused", "Finished", "Error"),
            d.var("STATE"))
    print("\n== Print Diagnostics")
    d.diagnostics()
    print(d.prints())
    if save and cloud.last_status:
        model = re.sub(r"[^A-Za-z0-9_]", "_", (d.prop("Model") or "washer").split(" ")[0])
        out = FIX / f"status_live_{model}.json"
        out.write_text(scrub(json.dumps(json.loads(cloud.last_status), indent=2, ensure_ascii=False)) + "\n",
                       encoding="utf-8", newline="\n")
        print(f"\nSaved {out} - check it for anything personal before committing.")
    errors = lua_to_py(d.M.errors) or []
    if errors:
        print("Driver error log:", *errors, sep="\n  ")
    return R.summary()


def run_watch(token, device):
    d, _ = connect(token, device)
    if not d.prop("Device ID"):
        sys.exit("No washer selected - pass --device <deviceId>")
    show(d)
    print("\n== Watching (Ctrl+C to stop). Start a cycle at the washer.")
    last = None
    n_events = len(d.events())
    try:
        while True:
            time.sleep(1)
            d.advance(1000)
            now = tuple(d.var(n) for n in VARIABLES)
            if now != last:
                print(time.strftime("%H:%M:%S"), ", ".join(f"{n}={v!r}" for n, v in zip(VARIABLES, now)))
                last = now
            for e in d.events()[n_events:]:
                print(time.strftime("%H:%M:%S"), "EVENT", e)
            n_events = len(d.events())
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


def run_pause_resume(token, device):
    R = Results()
    d, _ = connect(token, device)
    show(d)
    if d.var("STATE") != "Running" or d.var("REMOTE_CONTROL_ENABLED") != "1":
        sys.exit("Needs a running cycle with Smart Control on (STATE=Running, REMOTE_CONTROL_ENABLED=1).")
    print("\n== Pause")
    d.command("PAUSE")
    secs = wait_for(d, lambda: d.var("STATE") == "Paused" and d.icon() != "pending")
    R.check("pause confirmed", secs is not None, f"{secs:.0f}s" if secs is not None else d.prop("Driver Status"))
    print("\n== Resume")
    d.command("RESUME")
    secs = wait_for(d, lambda: d.var("STATE") == "Running" and d.icon() != "pending")
    R.check("resume confirmed", secs is not None, f"{secs:.0f}s" if secs is not None else d.prop("Driver Status"))
    if d.var("STATE") == "Paused":
        print("!! The washer is still paused: resume it at the washer or in the SmartThings app.")
    R.check("events", d.events() == ["Cycle Paused"], d.events())
    return R.summary()


def run_create_app(token, state_file):
    R = Results()
    d = Driver(luajit21, LiveCloud())
    d.init()
    d.set_property("Personal Access Token", token)
    print("\n== Create OAuth app via the driver")
    d.action("CreateOAuthApp")
    cid, secret = d.prop("OAuth Client ID"), d.prop("OAuth Client Secret")
    R.check("OAuth app created", cid and secret, d.prop("Authorization Status"))
    if cid:
        state = {"client_id": cid, "client_secret": secret, "redirect_uri": d.prop("OAuth Redirect URI"),
                 "authorization_url": d.prop("Authorization URL")}
        Path(state_file).write_text(json.dumps(state, indent=2), encoding="utf-8")
        print(f"\nSaved to {state_file} (keep it private)")
        print(f"Authorize URL : {state['authorization_url']}")
    return R.summary()


def run_oauth(state_file, redirected):
    R = Results()
    state = json.loads(Path(state_file).read_text(encoding="utf-8"))
    d = Driver(luajit21, LiveCloud())
    d.init()
    d.g.Properties["OAuth Client ID"] = state["client_id"]
    d.g.Properties["OAuth Client Secret"] = state["client_secret"]
    print("\n== Exchange authorization code")
    d.set_property("Authorization Code", redirected)
    auth = d.persisted_auth() or {}
    R.check("code exchanged for tokens", auth.get("access_token") and auth.get("refresh_token"),
            d.prop("Authorization Status"))
    R.check("discovery with OAuth token", d.prop("Device ID") != "", d.prop("Driver Status"))
    if not auth.get("refresh_token"):
        return R.summary()
    print("\n== Renew (refresh token rotation)")
    old_rt = auth["refresh_token"]
    d.action("RenewToken")
    new = d.persisted_auth() or {}
    R.check("token renewed", new.get("access_token") and new.get("access_token") != auth["access_token"],
            d.prop("Authorization Status"))
    R.check("refresh token rotated", new.get("refresh_token") and new["refresh_token"] != old_rt)
    print("\n== Status with renewed OAuth token")
    d.action("RefreshStatus")
    R.check("status read with OAuth token", d.var("STATE") != "", d.prop("Driver Status"))
    return R.summary()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["status", "watch", "pause-resume", "create-app", "oauth"])
    ap.add_argument("redirected", nargs="?", default="")
    ap.add_argument("--device", default="")
    ap.add_argument("--save", action="store_true")
    ap.add_argument("--state", default=str(Path(os.environ.get("TEMP", ".")) / "c4_samsung_washer_oauth_state.json"))
    args = ap.parse_args()
    token = os.environ.get("ST_TOKEN", "").strip()
    if not token and args.mode != "oauth":
        sys.exit("Set ST_TOKEN to a SmartThings personal access token (see README for the required scopes)")
    if args.mode == "status":
        return run_status(token, args.device, args.save)
    if args.mode == "watch":
        return run_watch(token, args.device)
    if args.mode == "pause-resume":
        return run_pause_resume(token, args.device)
    if args.mode == "create-app":
        return run_create_app(token, args.state)
    return run_oauth(args.state, args.redirected)


if __name__ == "__main__":
    sys.exit(main())
