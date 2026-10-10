"""Offline tests for src/driver.lua.

Runs the real driver under Lua 5.1 and LuaJIT 2.1 (via lupa) against a mocked
Control4 runtime (tests/c4_mock.lua) and a fake SmartThings cloud that mimics
the real API: rotating single-use refresh tokens, 401 on expired tokens, 422 on
disabled capabilities, delayed state changes, paginated device lists, health.

Fixtures in tests/fixtures (see NOTICE; tests/make_fixtures.py makes the derived ones):
  Recorded SmartThings answers (Home Assistant's test fixtures, Apache-2.0):
    da_wm_wm_01011         DA_WM_TP1_21_COMMON (WD7000B), the WW5000C platform: running, Smart Control on
    da_wm_wm_000001        DA_WM_TP2_20_COMMON (WF6300R): switched off, Smart Control off
    da_wm_wm_000001_1      DA_WM_A51_20_COMMON washer-dryer: running, washerOperatingState disabled
    da_wm_wm_100001        older TP6X (WA54M8750AV): ready, Smart Control off
    da_wm_wm_100002        older TP6X (WV60M9900AV): spinning, Smart Control on
    devices_da_wm_dw_000001, devices_da_wm_wd_000001   a dishwasher and a dryer (must not be listed)
  Derived from da_wm_wm_01011 (only the state attributes changed):
    da_wm_wm_01011_paused, _finished, _error, _ready_remote_off

Usage:  python tests/test_driver.py          (or: python -m pytest tests)
"""
import base64
import calendar
import json
import re
import sys
import time
import traceback
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from lupa import lua51, luajit21

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
FIX = Path(__file__).resolve().parent / "fixtures"

API = "https://api.smartthings.com/v1"
TOKEN_URL = "https://auth-global.api.smartthings.com/oauth/token"
PAT = "pat-0123456789"
DEG = "°"
SMART_CONTROL = "Press Smart Control on the washer first"
TILE = 5001

RECORDED = ["da_wm_wm_01011", "da_wm_wm_000001", "da_wm_wm_000001_1", "da_wm_wm_100001", "da_wm_wm_100002"]
DERIVED = ["da_wm_wm_01011_paused", "da_wm_wm_01011_finished", "da_wm_wm_01011_error",
           "da_wm_wm_01011_ready_remote_off"]
VARIABLES = ["STATE", "JOB_STATE", "PROGRAMME", "REMAINING_MINUTES", "FINISH_TIME", "PROGRESS_PERCENT", "SPIN",
             "TEMPERATURE", "KIDS_LOCK", "REMOTE_CONTROL_ENABLED", "LAST_FINISHED"]


def load_fixture(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def lua_to_py(v):
    if hasattr(v, "items") and not isinstance(v, (str, bytes, dict)):
        d = {k: lua_to_py(x) for k, x in v.items()}
        if d and all(isinstance(k, int) for k in d):
            return [d[i] for i in sorted(d)]
        return d
    return v


def device_of(fixture):
    name = fixture
    while not (FIX / f"devices_{name}.json").exists():
        name = name.rsplit("_", 1)[0]
    d = load_fixture(f"devices_{name}.json")
    return d["items"][0] if "items" in d else d


LIGHT = {"deviceId": "light-0001", "label": "Kitchen light", "presentationId": "light",
         "components": [{"id": "main", "capabilities": [{"id": "switch"}], "categories": [{"name": "Light"}]}]}
DISHWASHER = device_of("da_wm_dw_000001")
DRYER = device_of("da_wm_wd_000001")


def local_hhmm(ts):
    return time.strftime("%H:%M", time.localtime(ts))


# --------------------------------------------------------------------------
# Fake SmartThings cloud
# --------------------------------------------------------------------------
class FakeSmartThings:
    def __init__(self, fixture="da_wm_wm_01011", extra_devices=(LIGHT, DISHWASHER, DRYER), page_size=50):
        self.device = device_of(fixture)
        self.device_id = self.device["deviceId"]
        self.devices = [*extra_devices, self.device]
        self.status = load_fixture(f"status_{fixture}.json")
        self.page_size = page_size
        self.health = "ONLINE"
        self.client_id = None
        self.client_secret = None
        self.redirect_uris = []
        self.scopes = []
        self.codes = {}
        self.access_tokens = set()
        self.refresh_tokens = set()
        self.n = 0
        self.log = []
        self.apps_created = []
        self.token_requests = []
        self.commands = []
        self.flip_after_reads = 1   # status reads before a commanded change shows up
        self.ignore_commands = False
        self.pending = []           # [(apply_fn, reads_left)]
        self.network_down = False
        self.token_server_error = False
        self.status_code = None     # force an HTTP code on /status (e.g. 429)

    # helpers -------------------------------------------------------------
    def _id(self, prefix):
        self.n += 1
        return f"{prefix}-{self.n:04d}"

    def main(self):
        return self.status["components"]["main"]

    def get(self, cap, attr):
        return self.main()[cap][attr]["value"]

    def set(self, cap, attr, value):
        m = self.main()
        if cap in m:
            m[cap].setdefault(attr, {})["value"] = value

    def set_running(self, remaining=None, job="wash", progress=None):
        self.set("samsungce.washerOperatingState", "operatingState", "running")
        self.set("samsungce.washerOperatingState", "washerJobState", job)
        self.set("washerOperatingState", "machineState", "run")
        self.set("washerOperatingState", "washerJobState", job)
        if remaining is not None:
            self.set("samsungce.washerOperatingState", "remainingTime", remaining)
        if progress is not None:
            self.set("samsungce.washerOperatingState", "progress", progress)
        self.set("switch", "switch", "on")

    def set_paused(self):
        self.set("samsungce.washerOperatingState", "operatingState", "paused")
        self.set("washerOperatingState", "machineState", "pause")

    def set_ready(self, job="none"):
        self.set("samsungce.washerOperatingState", "operatingState", "ready")
        self.set("samsungce.washerOperatingState", "washerJobState", job)
        self.set("washerOperatingState", "machineState", "stop")
        self.set("washerOperatingState", "washerJobState", job)

    def set_remote(self, on):
        self.set("remoteControlStatus", "remoteControlEnabled", "true" if on else "false")

    def set_errors(self, events):
        if events is None:
            self.main().pop("samsungce.errorAndAlarmState", None)
        else:
            self.main()["samsungce.errorAndAlarmState"] = {"events": {"value": events}}

    def machine_commands(self):
        return [c["commands"][0]["arguments"][0] for c in self.commands
                if c["commands"][0]["command"] == "setMachineState"]

    def issue_tokens(self):
        at, rt = self._id("access"), self._id("refresh")
        self.access_tokens.add(at)
        self.refresh_tokens.add(rt)
        return {"access_token": at, "refresh_token": rt, "token_type": "bearer",
                "expires_in": 86399, "scope": " ".join(self.scopes), "installed_app_id": "iapp-1"}

    def expire_all_access_tokens(self):
        self.access_tokens.clear()

    def authorize(self, auth_url):
        """Simulate the user opening the authorization URL and approving."""
        u = urlparse(auth_url)
        assert f"{u.scheme}://{u.netloc}{u.path}" == "https://api.smartthings.com/oauth/authorize"
        q = parse_qs(u.query)
        assert q["client_id"] == [self.client_id], q
        assert q["response_type"] == ["code"]
        assert q["redirect_uri"][0] in self.redirect_uris
        assert set(q["scope"][0].split(" ")) == {"r:devices:*", "x:devices:*"}, q["scope"]
        code = self._id("code")
        self.codes[code] = q["redirect_uri"][0]
        return f"{q['redirect_uri'][0]}?code={code}"

    def _bearer_ok(self, headers):
        auth = headers.get("Authorization", "")
        tok = auth[len("Bearer "):] if auth.startswith("Bearer ") else None
        return tok in self.access_tokens or tok == PAT

    def _disabled(self, comp, cap):
        comps = self.status["components"]
        dcaps = comps.get(comp, {}).get("custom.disabledCapabilities", {}).get("disabledCapabilities", {}).get("value") or []
        return cap in dcaps or cap not in comps.get(comp, {})

    def _apply_command(self, c):
        comp, cap, cmd, args = c["component"], c["capability"], c["command"], c.get("arguments", [])
        assert comp == "main" and cap == "washerOperatingState" and cmd == "setMachineState", c
        assert len(args) == 1 and args[0] in ("run", "pause", "stop"), args
        if args[0] == "run":
            # The driver must never send run unless the washer reports Smart Control on.
            assert self.get("remoteControlStatus", "remoteControlEnabled") == "true", "run sent without Smart Control"
            return lambda: self.set_running()
        if args[0] == "pause":
            return self.set_paused
        return self.set_ready

    # request handler -----------------------------------------------------
    def handle(self, method, url, headers, data):
        self.log.append((method, url))
        if self.network_down:
            return None, None
        assert "User-Agent" in headers

        if url == TOKEN_URL:
            assert method == "POST"
            if self.token_server_error:
                return 503, "Service Unavailable"
            assert headers.get("Content-Type") == "application/x-www-form-urlencoded"
            basic = base64.b64decode(headers["Authorization"][len("Basic "):]).decode()
            if basic != f"{self.client_id}:{self.client_secret}":
                return 401, json.dumps({"error": "invalid_client"})
            form = {k: v[0] for k, v in parse_qs(data).items()}
            self.token_requests.append(form)
            if form["grant_type"] == "authorization_code":
                redirect = self.codes.pop(form["code"], None)
                if redirect is None or redirect != form.get("redirect_uri"):
                    return 400, json.dumps({"error": "invalid_grant", "error_description": "Invalid authorization code"})
                return 200, json.dumps(self.issue_tokens())
            if form["grant_type"] == "refresh_token":
                rt = form["refresh_token"]
                if rt not in self.refresh_tokens:
                    return 400, json.dumps({"error": "invalid_grant", "error_description": "Invalid refresh token"})
                self.refresh_tokens.discard(rt)  # single use
                return 200, json.dumps(self.issue_tokens())
            return 400, json.dumps({"error": "unsupported_grant_type"})

        assert url.startswith(API), url
        path = url[len(API):]

        if path == "/apps" and method == "POST":
            if headers.get("Authorization") != f"Bearer {PAT}":
                return 401, json.dumps({"error": {"code": "UnauthorizedError", "message": "bad token"}})
            body = json.loads(data)
            self.apps_created.append(body)
            assert body["appType"] == "API_ONLY"
            assert body["classifications"] == ["CONNECTED_SERVICE"]
            assert body["principalType"] == "LOCATION" and body["singleInstance"] is True
            assert body["apiOnly"] == {}
            assert isinstance(body["oauth"]["scope"], list) and isinstance(body["oauth"]["redirectUris"], list)
            assert re.fullmatch(r"[a-z0-9._-]{1,250}", body["appName"]), body["appName"]
            self.client_id, self.client_secret = "client-abc", "secret-xyz"
            self.redirect_uris = body["oauth"]["redirectUris"]
            self.scopes = body["oauth"]["scope"]
            return 200, json.dumps({"app": {"appId": "app-1", "appName": body["appName"]},
                                    "oauthClientId": self.client_id, "oauthClientSecret": self.client_secret})

        if not self._bearer_ok(headers):
            return 401, json.dumps({"requestId": "r1", "error": {"code": "UnauthorizedError", "message": "Unauthorized"}})

        if path.startswith("/devices") and "/" not in path[len("/devices"):] and method == "GET":
            q = parse_qs(urlparse(url).query)
            page = int(q.get("page", ["0"])[0])
            chunk = self.devices[page * self.page_size:(page + 1) * self.page_size]
            links = {}
            if (page + 1) * self.page_size < len(self.devices):
                links["next"] = {"href": f"{API}/devices?page={page + 1}"}
            return 200, json.dumps({"items": chunk, "_links": links})
        if path == f"/devices/{self.device_id}/status" and method == "GET":
            if self.status_code:
                return self.status_code, json.dumps({"error": {"message": "Too many requests"}})
            still = []
            for fn, reads_left in self.pending:
                if reads_left <= 0:
                    fn()
                else:
                    still.append((fn, reads_left - 1))
            self.pending = still
            return 200, json.dumps(self.status)
        if path == f"/devices/{self.device_id}/health" and method == "GET":
            return 200, json.dumps({"deviceId": self.device_id, "state": self.health})
        if path == f"/devices/{self.device_id}/commands" and method == "POST":
            assert headers.get("Content-Type") == "application/json"
            body = json.loads(data)
            self.commands.append(body)
            c = body["commands"][0]
            if self._disabled(c["component"], c["capability"]):
                return 422, json.dumps({"requestId": "r2", "error": {"code": "UnprocessableEntityError",
                                                                   "message": "capability disabled", "details": []}})
            fn = self._apply_command(c)
            if not self.ignore_commands:
                self.pending.append((fn, self.flip_after_reads))
            return 200, json.dumps({"results": [{"id": "c1", "status": "ACCEPTED"}]})
        return 404, json.dumps({"error": {"message": "not found: " + path}})

    def status_reads(self):
        return len([1 for m, u in self.log if u.endswith("/status")])


# --------------------------------------------------------------------------
# Driver harness
# --------------------------------------------------------------------------
def xml_root():
    return ET.parse(SRC / "driver.xml").getroot()


def xml_property_defaults():
    return {p.findtext("name"): p.findtext("default") or ""
            for p in xml_root().find("config").find("properties").findall("property")}


class Driver:
    def __init__(self, runtime_mod, cloud, persist_py=None):
        self.rt = runtime_mod.LuaRuntime(unpack_returned_tuples=True)
        self.cloud = cloud
        g = self.rt.globals()
        g.PY_B64 = lambda s: base64.b64encode(s.encode()).decode()
        self.rt.execute((Path(__file__).parent / "c4_mock.lua").read_text(encoding="utf-8"))
        g.Properties = self.rt.table_from(xml_property_defaults())
        self.rt.execute((SRC / "driver.lua").read_text(encoding="utf-8"))
        if persist_py is not None:   # simulate the persisted store surviving a reboot
            g.MOCK.persist = g.JSON.decode(json.dumps(persist_py))
        self.g = g
        self.M = g.MOCK

    def init(self):
        self.M.inInit = True
        self.g.OnDriverInit("DIT_ADDING")
        self.M.inInit = False
        self.g.OnDriverLateInit("DIT_ADDING")
        self.pump()

    def pump(self):
        while True:
            req = self.M.takeRequest()
            if req is None:
                return
            headers = dict(lua_to_py(req.headers) or {})
            assert req.opts.fail_on_error is False, "fail_on_error must be false"
            code, body = self.cloud.handle(req.method, req.url, headers, req.data)
            if code is None:
                self.M.respond(req, 0, "", 7, "Couldn't connect to server")
            else:
                self.M.respond(req, code, body)

    def advance(self, ms):
        target = self.M.ms + ms
        while True:
            t = self.M.nextDue(target)
            if t is None:
                break
            self.M.fire(t)
            self.pump()
        self.M.setMs(target)

    def set_property(self, name, value):
        self.g.Properties[name] = value
        self.g.OnPropertyChanged(name)
        self.pump()

    def action(self, name):
        self.g.ExecuteCommand("LUA_ACTION", self.rt.table_from({"ACTION": name}))
        self.pump()

    def command(self, name, **params):
        self.g.ExecuteCommand(name, self.rt.table_from(params))
        self.pump()

    def tap(self, binding=TILE):
        self.g.ReceivedFromProxy(binding, "SELECT", self.rt.table_from({}))
        self.pump()

    def prop(self, name):
        return self.g.Properties[name]

    def events(self):
        return lua_to_py(self.M.events) or []

    def var(self, name):
        return self.M.vars[name]

    def icon(self, binding=TILE):
        for i in range(len(self.M.proxy), 0, -1):
            p = self.M.proxy[i]
            if p.binding == binding and p.cmd == "ICON_CHANGED":
                return p.params.icon
        return None

    def icon_desc(self, binding=TILE):
        for i in range(len(self.M.proxy), 0, -1):
            p = self.M.proxy[i]
            if p.binding == binding and p.cmd == "ICON_CHANGED":
                return p.params.icon_description
        return None

    def persisted_auth(self):
        e = self.M.persist["ST_AUTH"]
        return e and lua_to_py(e.value)

    def cond(self, name, value, logic="EQUAL"):
        return self.g.TestCondition(name, self.rt.table_from({"LOGIC": logic, "VALUE": value}))

    def prints(self):
        return "\n".join(lua_to_py(self.M.prints) or [])

    def diagnostics(self):
        self.M.prints = self.rt.table_from({})
        self.action("PrintDiagnostics")
        return self.prints()


def configured_driver(rt_mod, cloud=None):
    """Driver taken through the full setup: PAT -> app -> authorize -> discover."""
    cloud = cloud or FakeSmartThings()
    d = Driver(rt_mod, cloud)
    d.init()
    d.set_property("Personal Access Token", PAT)
    d.action("CreateOAuthApp")
    d.set_property("Authorization Code", cloud.authorize(d.prop("Authorization URL")))
    return d, cloud


def pat_driver(rt_mod, fixture="da_wm_wm_01011", cloud=None):
    cloud = cloud or FakeSmartThings(fixture)
    d = Driver(rt_mod, cloud)
    d.init()
    d.set_property("Personal Access Token", PAT)
    d.action("DiscoverDevices")
    assert d.prop("Device ID") == cloud.device_id
    return d, cloud


ACTIVE_MS = 30 * 1000


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------
def test_json_codec_roundtrip(rt_mod):
    d = Driver(rt_mod, FakeSmartThings())

    def norm(v):  # Lua has no null and cannot tell [] from {}
        if isinstance(v, dict):
            return {k: norm(x) for k, x in v.items() if x is not None}
        if isinstance(v, list):
            return [norm(x) for x in v] if v else {}
        return v
    for f in sorted(FIX.glob("*.json")):
        text = f.read_text(encoding="utf-8")
        assert json.loads(d.g.JSON.encode(d.g.JSON.decode(text))) == norm(json.loads(text)), f.name
    s = d.g.JSON.encode(d.rt.eval('{commands = {{component = "main", capability = "x", command = "on", arguments = {"run"}}}}'))
    assert json.loads(s) == {"commands": [{"component": "main", "capability": "x", "command": "on", "arguments": ["run"]}]}
    assert d.g.JSON.decode('"a\\u00e9\\ud83d\\ude00\\n"') == "aé\U0001F600\n"
    for bad in ('{"a":1', '[1,2', '{"a" 1}', 'tru', '"abc'):
        try:
            d.g.JSON.decode(bad)
        except Exception:
            continue
        raise AssertionError("expected decode error for " + bad)


def test_iso_time_parsing(rt_mod):
    d = Driver(rt_mod, FakeSmartThings())
    for s, expected in [("2025-04-25T10:34:12Z", "2025-04-25T10:34:12Z"),
                        ("2025-04-25T10:34:12.345Z", "2025-04-25T10:34:12Z"),
                        ("2025-04-25T12:34:12+02:00", "2025-04-25T10:34:12Z"),
                        ("2024-02-29T23:59:59-0130", "2024-03-01T01:29:59Z"),
                        ("1999-12-31T00:00:00Z", "1999-12-31T00:00:00Z")]:
        want = calendar.timegm(time.strptime(expected, "%Y-%m-%dT%H:%M:%SZ"))
        assert d.g.ParseIsoUtc(s) == want, (s, d.g.ParseIsoUtc(s), want)
    assert d.g.ParseIsoUtc("not a time") is None and d.g.ParseIsoUtc(None) is None


def test_programme_names(rt_mod):
    d = Driver(rt_mod, FakeSmartThings())
    base = load_fixture("status_da_wm_wm_01011.json")
    for cycle, expected in [("Table_02_Course_1C", "Eco 40-60"), ("Table_02_Course_1B", "Cotton"),
                            ("Table_00_Course_5B", "Cotton"), ("Table_00_Course_D0", "Cotton"),
                            ("Table_02_Course_FF", "Course FF"), ("Table_09_Course_01", "Course 01")]:
        base["components"]["main"]["samsungce.washerCycle"]["washerCycle"]["value"] = cycle
        res = lua_to_py(d.g.ParseDeviceStatus(d.g.JSON.decode(json.dumps(base))))
        assert res["programme"] == expected, (cycle, res.get("programme"))
    # washerMode (the standard capability) when samsungce.washerCycle is not there
    del base["components"]["main"]["samsungce.washerCycle"]
    base["components"]["main"]["washerMode"] = {"washerMode": {"value": "spinDry"}}
    res = lua_to_py(d.g.ParseDeviceStatus(d.g.JSON.decode(json.dumps(base))))
    assert res["programme"] == "Spin Dry"


# fixture: STATE, JOB_STATE, PROGRAMME, REMAINING_MINUTES, PROGRESS_PERCENT, SPIN, TEMPERATURE, KIDS_LOCK,
#          REMOTE_CONTROL_ENABLED, Model
EXPECTED = {
    "da_wm_wm_01011": ("Running", "Washing", "Eco 40-60", "100", "40", "1000 rpm", f"40 {DEG}C", "0", "1",
                       "WD7000B - DA_WM_TP1_21_COMMON, firmware DA_WM_TP1_21_COMMON_30240927"),
    "da_wm_wm_01011_paused": ("Paused", "Washing", "Eco 40-60", "100", "40", "1000 rpm", f"40 {DEG}C", "0", "1", None),
    "da_wm_wm_01011_finished": ("Finished", "", "Eco 40-60", "0", "100", "1000 rpm", f"40 {DEG}C", "0", "1", None),
    "da_wm_wm_01011_error": ("Error", "", "Eco 40-60", "0", "0", "1000 rpm", f"40 {DEG}C", "0", "1", None),
    "da_wm_wm_01011_ready_remote_off": ("Ready", "", "Eco 40-60", "0", "0", "1000 rpm", f"40 {DEG}C", "0", "0", None),
    "da_wm_wm_000001": ("Off", "", "Normal", "0", "0", "High", "Warm", "0", "0",
                        "WF6300R - DA_WM_TP2_20_COMMON, firmware DA_WM_TP2_20_COMMON_30230804"),
    "da_wm_wm_000001_1": ("Running", "Washing", "Course E3", "31", "36", "1400 rpm", f"40 {DEG}C", "0", "1",
                          "WD7800N - DA_WM_A51_20_COMMON, firmware DA_WM_A51_20_COMMON_30230708"),
    "da_wm_wm_100001": ("Ready", "", "", "0", "0", "", "", "0", "0", "WA54M8750AV"),
    "da_wm_wm_100002": ("Running", "Spinning", "", "5", "0", "", "", "0", "1", "WV60M9900AV"),
}


def test_every_fixture(rt_mod):
    """Discover + read every fixture: the eleven variables, properties and tile."""
    for fx in RECORDED + DERIVED:
        d, cloud = pat_driver(rt_mod, fx)
        state, job, prog, left, progress, spin, temp, lock, remote, model = EXPECTED[fx]
        got = tuple(d.var(n) for n in VARIABLES[:4]) + (d.var("PROGRESS_PERCENT"), d.var("SPIN"), d.var("TEMPERATURE"),
                                                         d.var("KIDS_LOCK"), d.var("REMOTE_CONTROL_ENABLED"))
        assert got == (state, job, prog, left, progress, spin, temp, lock, remote), (fx, got)
        if model:
            assert d.prop("Model") == model, (fx, d.prop("Model"))
        assert d.var("LAST_FINISHED") == ""                       # never on a first read
        if state in ("Running", "Paused"):
            assert d.var("FINISH_TIME") == local_hhmm(d.M.now + int(left) * 60), (fx, d.var("FINISH_TIME"))
        else:
            assert d.var("FINISH_TIME") == "", (fx, d.var("FINISH_TIME"))
        assert d.icon() == state.lower(), (fx, d.icon())
        assert d.events() == [], (fx, d.events())                  # a first read fires nothing
        assert d.cond("STATE", state) and d.cond("REMOTE_CONTROL", "Enabled" if remote == "1" else "Disabled")
        expected_status = "Washer reports an error: 4C" if state == "Error" else "OK"
        assert d.prop("Driver Status") == expected_status, (fx, d.prop("Driver Status"))
        types = lua_to_py(d.M.varTypes)
        for n in VARIABLES:
            if types[n] == "NUMBER":
                float(d.var(n))
            if types[n] == "BOOL":
                assert d.var(n) in ("0", "1")


def test_reported_features_and_disabled_capabilities(rt_mod):
    d, _ = pat_driver(rt_mod, "da_wm_wm_01011")
    assert d.prop("Reported Features") == ("Pause/Resume/Stop/Start, Programme, Spin, Temperature, Rinses, "
                                           "Kids lock, Remote control, Delay end, Power"), d.prop("Reported Features")
    assert d.prop("Rinses") == "2" and d.prop("Kids Lock") == "Off" and d.prop("Delay End") == "Off"
    assert d.prop("Remote Control").startswith("Enabled") and d.prop("Power") == "0 W"
    # The washer-dryer lists washerOperatingState and samsungce.washerDelayEnd as disabled
    d, _ = pat_driver(rt_mod, "da_wm_wm_000001_1")
    assert "Pause/Resume/Stop/Start" not in d.prop("Reported Features")
    assert "Delay end" not in d.prop("Reported Features") and d.prop("Delay End") == "Not available"
    # Older TP6X: no programme, spin, temperature or kids lock in SmartThings
    d, _ = pat_driver(rt_mod, "da_wm_wm_100001")
    assert d.prop("Reported Features") == "Pause/Resume/Stop/Start, Remote control", d.prop("Reported Features")
    assert d.prop("Programme") == "Not available" and d.prop("Spin") == "Not available"


# --------------------------------------------------------------------------
# Setup, discovery, variables
# --------------------------------------------------------------------------
def test_full_setup_flow(rt_mod):
    d, cloud = configured_driver(rt_mod)
    app = cloud.apps_created[0]
    assert app["oauth"]["redirectUris"] == ["https://httpbin.org/get"]
    assert app["displayName"] == "DirectorLink Samsung Washer"
    assert app["appName"].startswith("directorlink-samsung-washer-")
    assert d.prop("OAuth Client ID") == "client-abc" and d.prop("OAuth Client Secret") == "secret-xyz"
    assert d.prop("Authorization Code") == ""
    auth = d.persisted_auth()
    assert auth["refresh_token"] in cloud.refresh_tokens and d.M.persist["ST_AUTH"].enc is True
    assert "Authorized (OAuth)" in d.prop("Authorization Status")
    # discovery skipped the light, the dishwasher and the dryer, and auto-selected the washer
    assert d.prop("Device ID") == cloud.device_id
    listed = d.M.plist["Select Washer"]
    assert listed == "Machine à Laver (b854ca5f)", listed
    assert d.prop("Connection") == "Online" and d.prop("Driver Status") == "OK"
    assert d.var("STATE") == "Running" and d.icon() == "running"
    assert d.icon_desc() == f"Washer: Washing - Eco 40-60, 1 h 40 min left, done {local_hhmm(d.M.now + 6000)}", d.icon_desc()
    assert d.events() == []        # first observation fires nothing
    joined = d.prints() + d.diagnostics()
    # (The Client ID is not a secret: it is part of the authorization link printed for the installer.)
    for secret in [auth["access_token"], auth["refresh_token"], "secret-xyz", PAT]:
        assert secret not in joined, "secret leaked to log: " + secret


def test_discovery_lists_only_washers(rt_mod):
    d = Driver(rt_mod, FakeSmartThings())
    d.init()
    for dev in (LIGHT, DISHWASHER, DRYER):
        assert not d.g.IsWasher(d.g.JSON.decode(json.dumps(dev))), dev["label"]
    for fx in RECORDED:
        assert d.g.IsWasher(d.g.JSON.decode(json.dumps(device_of(fx)))), fx
    # deviceTypeName or category alone is enough; "Dishwasher" is not a washer
    assert d.g.IsWasher(d.g.JSON.decode('{"deviceTypeName": "Samsung OCF Washer"}'))
    assert not d.g.IsWasher(d.g.JSON.decode('{"deviceTypeName": "Samsung OCF Dishwasher"}'))
    assert d.g.IsWasher(d.g.JSON.decode('{"components": [{"id": "main", "categories": [{"name": "Washer"}]}]}'))


def test_discovery_two_washers_and_pagination(rt_mod):
    extra = [dict(LIGHT, deviceId=f"light-{i}") for i in range(7)] + [DISHWASHER, device_of("da_wm_wm_100002")]
    cloud = FakeSmartThings("da_wm_wm_01011", extra_devices=extra, page_size=3)
    d = Driver(rt_mod, cloud)
    d.init()
    d.set_property("Personal Access Token", PAT)
    d.action("DiscoverDevices")
    items = d.M.plist["Select Washer"].split(",")
    assert items == ["(run Discover Washers)", "Washer 2 (C097276D)", "Machine à Laver (b854ca5f)"], items
    assert d.prop("Device ID") == "" and "Select your washer" in d.prop("Driver Status")
    d.set_property("Select Washer", items[2])
    assert d.prop("Device ID") == cloud.device_id and d.prop("Driver Status") == "OK"


def test_variables_order_ids_and_types(rt_mod):
    d = Driver(rt_mod, FakeSmartThings())
    d.init()
    assert lua_to_py(d.M.varOrder) == VARIABLES
    ids = lua_to_py(d.M.varIds)
    assert [ids[n] for n in VARIABLES] == list(range(1001, 1012))
    types = lua_to_py(d.M.varTypes)
    assert {n: types[n] for n in VARIABLES} == {
        "STATE": "STRING", "JOB_STATE": "STRING", "PROGRAMME": "STRING", "REMAINING_MINUTES": "NUMBER",
        "FINISH_TIME": "STRING", "PROGRESS_PERCENT": "NUMBER", "SPIN": "STRING", "TEMPERATURE": "STRING",
        "KIDS_LOCK": "BOOL", "REMOTE_CONTROL_ENABLED": "BOOL", "LAST_FINISHED": "STRING"}
    assert d.var("STATE") == "" and d.icon() == "unknown" and d.icon_desc() == "Washer: not set up"


def test_variables_set_only_on_change(rt_mod):
    d, cloud = configured_driver(rt_mod)
    # A real washer counts its remaining time down; the fake's stays put, so FINISH_TIME (now + remaining)
    # would move with the clock. Leave it out.
    sets = lambda: {k: v for k, v in (lua_to_py(d.M.varSets) or {}).items() if k != "FINISH_TIME"}
    before = sets()
    d.advance(5 * ACTIVE_MS)                                  # five polls, nothing changed
    assert sets() == before
    cloud.set("samsungce.washerOperatingState", "remainingTime", 99)
    d.advance(ACTIVE_MS)
    after = sets()
    assert after["REMAINING_MINUTES"] == before["REMAINING_MINUTES"] + 1
    assert after["STATE"] == before["STATE"] and after["PROGRAMME"] == before["PROGRAMME"]


# --------------------------------------------------------------------------
# Cycle tracking and events
# --------------------------------------------------------------------------
def test_cycle_lifecycle_events(rt_mod):
    d, cloud = pat_driver(rt_mod, "da_wm_wm_01011_ready_remote_off")
    assert d.var("STATE") == "Ready" and d.var("REMOTE_CONTROL_ENABLED") == "0"
    cloud.set_remote(True)                                    # Smart Control pressed
    d.advance(ACTIVE_MS)
    assert d.events() == ["Remote Control Enabled"] and d.var("REMOTE_CONTROL_ENABLED") == "1"
    cloud.set_running(remaining=100, progress=1)              # started at the washer
    d.advance(ACTIVE_MS)
    assert d.events()[-1] == "Cycle Started" and d.var("STATE") == "Running" and d.var("JOB_STATE") == "Washing"
    cloud.set_paused()
    d.advance(ACTIVE_MS)
    assert d.events()[-1] == "Cycle Paused" and d.var("STATE") == "Paused" and d.icon() == "paused"
    cloud.set_running(remaining=60, job="rinse")              # resumed: no new Cycle Started
    d.advance(ACTIVE_MS)
    assert d.events()[-1] == "Cycle Paused" and d.var("JOB_STATE") == "Rinsing"
    cloud.set_running(remaining=1, job="spin", progress=99)
    d.advance(ACTIVE_MS)
    assert d.var("JOB_STATE") == "Spinning" and d.var("REMAINING_MINUTES") == "1"
    cloud.set_ready(job="finish")
    cloud.set("samsungce.washerOperatingState", "progress", 100)
    cloud.set("samsungce.washerOperatingState", "remainingTime", 0)
    d.advance(ACTIVE_MS)
    assert d.events() == ["Remote Control Enabled", "Cycle Started", "Cycle Paused", "Cycle Finished"], d.events()
    assert d.var("STATE") == "Finished" and d.icon() == "finished" and d.cond("STATE", "Finished")
    finished_at = d.var("LAST_FINISHED")
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", finished_at), finished_at
    assert finished_at == time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(d.M.now))
    assert d.var("FINISH_TIME") == local_hhmm(d.M.now) and d.var("PROGRESS_PERCENT") == "100"
    assert d.icon_desc() == f"Washer: Finished at {local_hhmm(d.M.now)}"
    # Stays Finished, never repeated: the job state clears, the washer switches itself off
    d.advance(10 * ACTIVE_MS)
    cloud.set_ready(job="none")
    d.advance(5 * ACTIVE_MS)
    cloud.set("switch", "switch", "off")
    d.advance(10 * 60 * 1000)
    assert d.var("STATE") == "Finished" and d.events().count("Cycle Finished") == 1
    assert d.var("LAST_FINISHED") == finished_at
    # Someone switches the washer on again: Finished is over
    cloud.set("switch", "switch", "on")
    d.advance(2 * 60 * 1000)
    assert d.var("STATE") == "Ready" and d.var("LAST_FINISHED") == finished_at
    assert d.events().count("Cycle Finished") == 1


def test_finish_missed_between_polls(rt_mod):
    d, cloud = pat_driver(rt_mod, "da_wm_wm_01011")
    cloud.set_running(remaining=2)
    d.advance(ACTIVE_MS)
    cloud.set_ready(job="none")                               # "finish" came and went between two reads
    d.advance(ACTIVE_MS)
    assert d.events() == ["Cycle Finished"] and d.var("STATE") == "Finished"


def test_finished_while_offline_after_the_end(rt_mod):
    d, cloud = pat_driver(rt_mod, "da_wm_wm_01011")
    cloud.set_running(remaining=1)
    d.advance(ACTIVE_MS)
    cloud.health = "OFFLINE"                                  # switched itself off right after the end
    d.advance(ACTIVE_MS)
    assert d.events() == ["Cycle Finished"] and d.var("STATE") == "Finished"


def test_cancelled_at_the_washer_is_not_finished(rt_mod):
    d, cloud = pat_driver(rt_mod, "da_wm_wm_01011")
    cloud.set_ready(job="none")                               # 100 minutes left, then stopped at the washer
    d.advance(ACTIVE_MS)
    assert d.var("STATE") == "Ready" and d.events() == [] and d.var("LAST_FINISHED") == ""
    cloud.set_running(remaining=90)
    d.advance(ACTIVE_MS)
    assert d.events() == ["Cycle Started"]


def test_restart_never_repeats_and_never_misses(rt_mod):
    # Restart after the finish: nothing repeats, LAST_FINISHED and STATE are back at once
    d, cloud = pat_driver(rt_mod, "da_wm_wm_01011")
    cloud.set_ready(job="finish")
    d.advance(ACTIVE_MS)
    assert d.events() == ["Cycle Finished"]
    last = d.var("LAST_FINISHED")
    d2 = Driver(rt_mod, cloud, persist_py=lua_to_py(d.M.persist))
    for k in ("Device ID", "Personal Access Token"):
        d2.g.Properties[k] = d.prop(k)
    d2.M.now = d.M.now
    d2.M.start = d.M.now
    d2.init()
    assert d2.var("STATE") == "Finished" and d2.var("LAST_FINISHED") == last and d2.icon() == "finished"
    d2.advance(6000 + 5 * ACTIVE_MS)
    assert d2.events() == [] and d2.var("LAST_FINISHED") == last
    # Restart in the middle of a cycle that finishes while the driver is down: announced once
    d, cloud = pat_driver(rt_mod, "da_wm_wm_01011")
    d.advance(ACTIVE_MS)
    cloud.set_ready(job="finish")
    d2 = Driver(rt_mod, cloud, persist_py=lua_to_py(d.M.persist))
    for k in ("Device ID", "Personal Access Token"):
        d2.g.Properties[k] = d.prop(k)
    d2.init()
    assert d2.var("STATE") == "Running"                       # last known, until the first read
    d2.advance(6000)
    assert d2.events() == ["Cycle Finished"] and d2.var("STATE") == "Finished"
    d2.advance(10 * ACTIVE_MS)
    assert d2.events() == ["Cycle Finished"]
    # Another washer selected: the cycle memory is not carried over
    d2.set_property("Device ID", "another-washer")
    assert d2.var("STATE") == "" and d2.var("LAST_FINISHED") == ""


def test_show_finished_for(rt_mod):
    d, cloud = pat_driver(rt_mod, "da_wm_wm_01011")
    cloud.set_ready(job="finish")
    d.advance(ACTIVE_MS)
    cloud.set_ready(job="none")                               # still on, not switched off
    d.advance(119 * 60 * 1000)
    assert d.var("STATE") == "Finished"
    d.advance(2 * 60 * 1000)
    assert d.var("STATE") == "Ready" and d.events() == ["Cycle Finished"]
    # 0 = until the washer is switched on again
    d, cloud = pat_driver(rt_mod, "da_wm_wm_01011")
    d.set_property("Show Finished For (minutes)", "0")
    cloud.set_ready(job="finish")
    d.advance(ACTIVE_MS)
    cloud.set_ready(job="none")
    cloud.set("switch", "switch", "off")
    d.advance(24 * 3600 * 1000)
    assert d.var("STATE") == "Finished"
    cloud.set("switch", "switch", "on")
    d.advance(2 * 60 * 1000)
    assert d.var("STATE") == "Ready"


def test_error_event_once_and_recovery(rt_mod):
    d, cloud = pat_driver(rt_mod, "da_wm_wm_01011")
    cloud.set_paused()
    cloud.set_errors(["dE"])                                  # door error: the washer pauses
    d.advance(ACTIVE_MS)
    assert d.events() == ["Error"] and d.var("STATE") == "Error" and d.icon() == "error"
    assert d.icon_desc() == "Washer: Error dE" and d.prop("Driver Status") == "Washer reports an error: dE"
    assert d.var("JOB_STATE") == "Washing" and d.var("REMAINING_MINUTES") == "100"   # still in the cycle
    d.advance(5 * ACTIVE_MS)
    assert d.events() == ["Error"]
    cloud.set_errors([])                                      # fixed, resumed: same cycle
    cloud.set_running()
    d.advance(ACTIVE_MS)
    assert d.events() == ["Error"] and d.var("STATE") == "Running" and d.prop("Driver Status") == "OK"


def test_error_shapes(rt_mod):
    d = Driver(rt_mod, FakeSmartThings())
    base = load_fixture("status_da_wm_wm_01011.json")
    main = base["components"]["main"]
    cases = [
        ({"samsungce.errorAndAlarmState": {"events": {"value": []}}}, []),
        ({"samsungce.errorAndAlarmState": {"events": {"value": ["4C"]}}}, ["4C"]),
        ({"samsungce.errorAndAlarmState": {"events": {"value": [{"eventId": "dE", "status": "on"}]}}}, ["dE"]),
        ({"samsungce.errorAndAlarmState": {"events": {"value": [{"eventId": "dE", "status": "off"}]}}}, []),
        ({"custom.error": {"error": {"value": None}}}, []),
        ({"custom.error": {"error": {"value": "none"}}}, []),
        ({"custom.error": {"error": {"value": "5C"}}}, ["5C"]),
    ]
    for extra, expected in cases:
        st = json.loads(json.dumps(base))
        st["components"]["main"].update(extra)
        res = lua_to_py(d.g.ParseDeviceStatus(d.g.JSON.decode(json.dumps(st))))
        assert (res.get("errors") or []) == expected, (extra, res.get("errors"))
    assert "samsungce.errorAndAlarmState" not in main


def test_remote_control_events(rt_mod):
    d, cloud = pat_driver(rt_mod, "da_wm_wm_01011")
    cloud.set_remote(False)
    d.advance(ACTIVE_MS)
    cloud.set_remote(True)
    d.advance(ACTIVE_MS)
    d.advance(3 * ACTIVE_MS)
    assert d.events() == ["Remote Control Disabled", "Remote Control Enabled"]
    assert d.cond("REMOTE_CONTROL", "Enabled") and not d.cond("REMOTE_CONTROL", "Enabled", "NOT_EQUAL")


# --------------------------------------------------------------------------
# Control
# --------------------------------------------------------------------------
def test_start_refused_without_smart_control_and_never_retried(rt_mod):
    for fx in ("da_wm_wm_01011_ready_remote_off", "da_wm_wm_100001"):
        d, cloud = pat_driver(rt_mod, fx)
        d.command("START")
        assert cloud.commands == [], fx
        assert d.prop("Driver Status") == f"Start refused: {SMART_CONTROL}", (fx, d.prop("Driver Status"))
        assert d.events() == ["Command Failed"]
        d.advance(60 * 1000)
        assert d.prop("Driver Status") == f"Start refused: {SMART_CONTROL}"    # stays visible a while
        cloud.set_remote(True)                                # Smart Control pressed later
        d.advance(30 * 60 * 1000)
        assert cloud.commands == [], "Start must never be retried by the driver"
        assert d.var("STATE") == "Ready" and d.prop("Driver Status") == "OK"
    # The Composer action behaves the same
    d, cloud = pat_driver(rt_mod, "da_wm_wm_01011_ready_remote_off")
    d.action("Start")
    assert cloud.commands == [] and SMART_CONTROL in d.prop("Driver Status")


def test_start_decides_on_a_fresh_read(rt_mod):
    d, cloud = pat_driver(rt_mod, "da_wm_wm_01011_ready_remote_off")
    cloud.set_remote(True)                                    # pressed just now, not read yet
    assert d.var("REMOTE_CONTROL_ENABLED") == "0"
    d.command("START")
    assert cloud.machine_commands() == ["run"]
    d, cloud = pat_driver(rt_mod, "da_wm_wm_01011_ready_remote_off")
    cloud.set_remote(True)
    d.advance(ACTIVE_MS)
    cloud.set_remote(False)                                   # switched off again since the last read
    d.command("START")
    assert cloud.commands == [] and SMART_CONTROL in d.prop("Driver Status")


def test_start_with_smart_control(rt_mod):
    d, cloud = pat_driver(rt_mod, "da_wm_wm_01011_ready_remote_off")
    cloud.set_remote(True)
    d.advance(ACTIVE_MS)
    d.command("START")
    assert cloud.commands[-1] == {"commands": [{"component": "main", "capability": "washerOperatingState",
                                                "command": "setMachineState", "arguments": ["run"]}]}
    assert d.icon() == "pending" and d.icon_desc() == "Washer: starting..."
    d.advance(10000)
    assert d.var("STATE") == "Running" and d.icon() == "running"
    assert d.events() == ["Remote Control Enabled", "Cycle Started"] and d.prop("Driver Status") == "Start done"


def test_pause_resume_stop(rt_mod):
    d, cloud = configured_driver(rt_mod)
    d.action("Pause")
    assert cloud.machine_commands() == ["pause"]
    d.advance(10000)
    assert d.var("STATE") == "Paused" and d.events() == ["Cycle Paused"]
    d.command("RESUME")
    d.advance(10000)
    assert cloud.machine_commands() == ["pause", "run"] and d.var("STATE") == "Running"
    assert d.events() == ["Cycle Paused"]                     # resuming is not a new cycle
    cloud.set("samsungce.washerOperatingState", "remainingTime", 2)
    d.advance(ACTIVE_MS)
    d.command("STOP")
    d.advance(10000)
    assert cloud.machine_commands() == ["pause", "run", "stop"]
    assert d.var("STATE") == "Ready" and "Cycle Finished" not in d.events()   # stopped, not finished
    assert d.var("LAST_FINISHED") == ""


def test_resume_refused_without_smart_control(rt_mod):
    d, cloud = pat_driver(rt_mod, "da_wm_wm_01011_paused")
    cloud.set_remote(False)
    d.command("RESUME")
    assert cloud.commands == [] and d.prop("Driver Status") == f"Resume refused: {SMART_CONTROL}"
    d.command("STOP")                                         # stopping needs no Smart Control
    assert cloud.machine_commands() == ["stop"]


def test_commands_refused_in_the_wrong_state(rt_mod):
    cases = [
        ("da_wm_wm_01011_ready_remote_off", "PAUSE", "Pause refused: the washer is not running"),
        ("da_wm_wm_01011_ready_remote_off", "STOP", "Stop refused: no cycle is running"),
        ("da_wm_wm_01011", "START", "Start refused: the washer is already running"),
        ("da_wm_wm_01011", "RESUME", "Resume refused: the washer is not paused"),
        ("da_wm_wm_01011_paused", "START", "Start refused: the cycle is paused - use Resume"),
        ("da_wm_wm_000001", "START", "Start refused: the washer is switched off"),
        ("da_wm_wm_01011_error", "RESUME", "Resume refused: the washer reports an error (4C) - check the washer"),
        ("da_wm_wm_000001_1", "PAUSE", "Pause refused: this washer does not accept it through SmartThings "
                                       "(washerOperatingState disabled for this model)"),
    ]
    for fx, cmd, message in cases:
        d, cloud = pat_driver(rt_mod, fx)
        d.command(cmd)
        assert cloud.commands == [], (fx, cmd)
        assert d.prop("Driver Status") == message, (fx, cmd, d.prop("Driver Status"))
        assert d.events() == ["Command Failed"], (fx, cmd)
    d, cloud = pat_driver(rt_mod, "da_wm_wm_01011")
    cloud.health = "OFFLINE"
    d.command("PAUSE")
    assert cloud.commands == [] and d.prop("Driver Status") == "Pause refused: the washer is offline in SmartThings"


def test_command_not_confirmed_or_rejected(rt_mod):
    d, cloud = configured_driver(rt_mod)
    cloud.ignore_commands = True
    d.command("PAUSE")
    d.advance(60000)
    assert d.events() == ["Command Failed"] and d.prop("Driver Status") == "Pause was not confirmed by the washer"
    assert d.var("STATE") == "Running" and d.icon() == "running"
    assert cloud.machine_commands() == ["pause"]              # never re-sent
    # A command SmartThings rejects (HTTP 422): reported, not retried
    d, cloud = configured_driver(rt_mod)
    cloud._disabled = lambda comp, cap: True
    d.command("PAUSE")
    assert d.events() == ["Command Failed"] and d.prop("Driver Status").startswith("Pause: the washer refused it")
    d.advance(60000)
    assert cloud.machine_commands() == ["pause"] and d.icon() == "running"


def test_never_changes_programme_temperature_or_spin(rt_mod):
    lua = (SRC / "driver.lua").read_text(encoding="utf-8")
    calls = re.findall(r'sendCommand\("(\w+)", "([\w.]+)", "(\w+)"', lua)
    assert calls == [("main", "washerOperatingState", "setMachineState")], calls
    for forbidden in ("setWasherCycle", "setWasherSpinLevel", "setWasherWaterTemperature", "setWasherRinseCycles",
                      "setWasherMode", "setLockState", "setDelayEndTime"):
        assert forbidden not in lua, forbidden


# --------------------------------------------------------------------------
# Tile
# --------------------------------------------------------------------------
def test_tile_tap_only_reads(rt_mod):
    d = Driver(rt_mod, FakeSmartThings())
    d.init()
    d.tap()
    assert d.icon() == "unknown" and d.events() == []         # not set up: nothing happens
    for fx in ("da_wm_wm_01011", "da_wm_wm_01011_paused", "da_wm_wm_01011_ready_remote_off", "da_wm_wm_01011_finished"):
        d, cloud = pat_driver(rt_mod, fx)
        reads = cloud.status_reads()
        d.g.ReceivedFromProxy(TILE, "SELECT", d.rt.table_from({}))
        assert d.icon() == "pending" and d.icon_desc() == "Washer: refreshing..."
        d.pump()
        assert cloud.status_reads() == reads + 1 and cloud.commands == [], fx
        assert d.icon() == EXPECTED[fx][0].lower()
        for _ in range(5):
            d.tap()
        assert cloud.commands == [], fx


def test_tile_problems(rt_mod):
    d, cloud = configured_driver(rt_mod)
    cloud.network_down = True
    d.tap()
    assert d.icon() == "running" and d.icon_desc().endswith("(status refresh failed)")
    cloud.network_down = False
    d.tap()
    assert d.icon_desc().endswith("(status refresh failed)") is False


# --------------------------------------------------------------------------
# Polling, offline, rate limits
# --------------------------------------------------------------------------
def test_polling_fast_while_on_slow_while_off(rt_mod):
    d, cloud = pat_driver(rt_mod, "da_wm_wm_01011")
    n = cloud.status_reads()
    d.advance(5 * 60 * 1000)
    assert cloud.status_reads() - n == 10                     # every 30 s while running
    cloud.set_ready()
    cloud.set("switch", "switch", "off")
    d.advance(ACTIVE_MS)
    assert d.var("STATE") == "Off"
    n = cloud.status_reads()
    d.advance(10 * 60 * 1000)
    assert cloud.status_reads() - n == 5                      # every 2 minutes while off
    d.set_property("Poll Interval When Off (minutes)", "5")
    n = cloud.status_reads()
    d.advance(20 * 60 * 1000)
    assert cloud.status_reads() - n == 4
    # Each poll is one status and one health read: 4 requests a minute at most while on
    d, cloud = pat_driver(rt_mod, "da_wm_wm_01011")
    n = len(cloud.log)
    d.advance(60 * 1000)
    assert len(cloud.log) - n == 4


def test_rate_limited_pauses_polling(rt_mod):
    d, cloud = pat_driver(rt_mod, "da_wm_wm_01011")
    cloud.status_code = 429
    d.advance(ACTIVE_MS)
    assert "Rate limited by SmartThings" in d.prop("Driver Status")
    n = cloud.status_reads()
    d.advance(100 * 1000)
    assert cloud.status_reads() == n                          # no polling for two minutes
    cloud.status_code = None
    d.advance(60 * 1000)
    assert cloud.status_reads() > n and d.prop("Driver Status") == "OK"


def test_offline_mid_cycle_keeps_the_cycle(rt_mod):
    d, cloud = pat_driver(rt_mod, "da_wm_wm_01011")
    cloud.set_running(remaining=40)
    d.advance(ACTIVE_MS)
    cloud.health = "OFFLINE"                                  # Wi-Fi drops mid-cycle
    d.advance(5 * ACTIVE_MS)
    assert d.var("STATE") == "Running" and d.prop("State") == "Running (offline, last known)"
    assert d.icon_desc().endswith("(offline, last known)") and d.events() == []
    cloud.health = "ONLINE"
    d.advance(ACTIVE_MS)
    assert d.var("STATE") == "Running" and d.events() == []   # no second Cycle Started
    # Offline for good: after its finish time and a grace period, Off without Cycle Finished
    cloud.health = "OFFLINE"
    d.advance(60 * 60 * 1000)
    assert d.var("STATE") == "Off" and d.events() == [] and d.var("LAST_FINISHED") == ""
    assert d.prop("Driver Status") == "Washer offline in SmartThings (switched off or not on Wi-Fi)"


def test_offline_while_idle_is_off(rt_mod):
    d, cloud = pat_driver(rt_mod, "da_wm_wm_01011_ready_remote_off")
    cloud.health = "OFFLINE"
    d.advance(ACTIVE_MS)
    assert d.var("STATE") == "Off" and d.icon() == "off" and d.prop("Connection") == "Offline"
    assert d.events() == []


# --------------------------------------------------------------------------
# Diagnostics
# --------------------------------------------------------------------------
def test_print_diagnostics(rt_mod):
    d, cloud = configured_driver(rt_mod, FakeSmartThings("da_wm_wm_000001_1"))
    out = d.diagnostics()
    assert "===== DirectorLink · Samsung Washer - diagnostics =====" in out
    assert "on Control4 OS 4.2.1.000000" in out
    assert "samsungce.washerOperatingState.operatingState = \"running\"" in out
    assert "samsungce.washerCycle.washerCycle = \"Table_00_Course_E3\" -> Course E3" in out
    assert "washerOperatingState.machineState = \"run\" [disabled for this model]" in out
    assert "custom.washerSpinLevel.washerSpinLevel = \"1400\"" in out
    assert "not reported: washerMode, samsungce.errorAndAlarmState, custom.error" in out, out
    assert "Other capabilities (names only):" in out and "sec.wifiConfiguration" in out
    assert "Control       : not available - washerOperatingState disabled for this model" in out
    assert "Personal Access Token set" in out and "refresh token saved" in out
    auth = d.persisted_auth()
    for secret in (auth["access_token"], auth["refresh_token"], "secret-xyz", PAT, "client-abc",
                   cloud.device_id, "REDACTED", "WD7800N/DC92"):
        assert secret not in out, "must not be printed: " + secret
    assert cloud.device_id[:8] in out
    # Events are listed
    cloud.set_ready(job="finish")
    d.advance(ACTIVE_MS)
    assert "Cycle Finished" in d.diagnostics()


def test_debug_log_has_no_secrets(rt_mod):
    cloud = FakeSmartThings()
    d = Driver(rt_mod, cloud)
    d.init()
    d.set_property("Debug Mode", "On")
    d.set_property("Personal Access Token", PAT)
    d.action("CreateOAuthApp")
    d.set_property("Authorization Code", cloud.authorize(d.prop("Authorization URL")))
    cloud.expire_all_access_tokens()
    d.action("RenewToken")
    d.command("PAUSE")
    d.advance(60000)
    out = d.prints()
    assert ">> GET" in out                                    # debug really on
    for t in list(cloud.access_tokens) + list(cloud.refresh_tokens) + ["secret-xyz", PAT]:
        assert t not in out, "leaked: " + t


# --------------------------------------------------------------------------
# SmartThings login (carried over from DirectorLink · Samsung Refrigerator)
# --------------------------------------------------------------------------
def test_unconfigured_startup(rt_mod):
    d = Driver(rt_mod, FakeSmartThings())
    d.init()
    assert "Not configured" in d.prop("Driver Status")
    assert d.prop("Authorization Status") == "Not authorized"
    d.command("PAUSE")
    assert d.events() == ["Command Failed"] and "not authorized" in d.prop("Driver Status")


def test_expired_access_token_renews_and_retries(rt_mod):
    d, cloud = configured_driver(rt_mod)
    old_refresh = d.persisted_auth()["refresh_token"]
    cloud.expire_all_access_tokens()
    d.command("PAUSE")
    d.advance(10000)
    assert cloud.get("samsungce.washerOperatingState", "operatingState") == "paused"
    new = d.persisted_auth()
    assert new["refresh_token"] != old_refresh and new["refresh_token"] in cloud.refresh_tokens
    assert [r["grant_type"] for r in cloud.token_requests] == ["authorization_code", "refresh_token"]


def test_concurrent_401s_share_one_refresh(rt_mod):
    d, cloud = configured_driver(rt_mod)
    cloud.expire_all_access_tokens()
    d.g.ExecuteCommand("REFRESH", d.rt.table_from({}))
    d.g.ExecuteCommand("LUA_ACTION", d.rt.table_from({"ACTION": "DiscoverDevices"}))
    d.g.ExecuteCommand("PAUSE", d.rt.table_from({}))
    d.pump()
    assert len([r for r in cloud.token_requests if r["grant_type"] == "refresh_token"]) == 1
    d.advance(10000)
    assert d.var("STATE") == "Paused"


def test_proactive_renewal_and_restart(rt_mod):
    d, cloud = configured_driver(rt_mod)
    d.advance(21 * 3600 * 1000)
    assert len([r for r in cloud.token_requests if r["grant_type"] == "refresh_token"]) >= 1
    assert d.persisted_auth()["expires_at"] - d.M.now > 20 * 3600
    d2 = Driver(rt_mod, cloud, persist_py=lua_to_py(d.M.persist))
    for k in ("Device ID", "OAuth Client ID", "OAuth Client Secret"):
        d2.g.Properties[k] = d.prop(k)
    d2.init()
    d2.advance(6000)
    assert d2.prop("Connection") == "Online" and d2.prop("Driver Status") == "OK"
    assert d2.events() == []


def test_renewal_rejected_requires_reauth(rt_mod):
    d, cloud = configured_driver(rt_mod)
    cloud.refresh_tokens.clear()
    d.action("RenewToken")
    assert "Authorization Required" in d.events()
    assert "Re-authorization required" in d.prop("Authorization Status")
    assert d.icon() == "error" and d.persisted_auth() in ({}, None, [])
    d.set_property("Authorization Code", cloud.authorize(d.prop("Authorization URL")))
    assert d.prop("Authorization Status").startswith("Authorized") and d.icon() == "running"


def test_transient_renewal_failure_retries(rt_mod):
    d, cloud = configured_driver(rt_mod)
    rt_before = d.persisted_auth()["refresh_token"]
    cloud.token_server_error = True
    d.action("RenewToken")
    assert "retrying" in d.prop("Authorization Status")
    assert d.persisted_auth()["refresh_token"] == rt_before and "Authorization Required" not in d.events()
    cloud.token_server_error = False
    d.advance(5 * 60 * 1000 + 1)
    assert d.persisted_auth()["refresh_token"] != rt_before
    assert d.prop("Authorization Status").startswith("Authorized")


def test_network_down(rt_mod):
    d, cloud = configured_driver(rt_mod)
    cloud.network_down = True
    d.command("PAUSE")
    assert d.events() == ["Command Failed"] and "Network error" in d.prop("Driver Status")
    assert d.persisted_auth()["refresh_token"] in cloud.refresh_tokens


def test_auth_code_formats(rt_mod):
    for fmt in ("url", "raw", "httpbin_json"):
        cloud = FakeSmartThings()
        d = Driver(rt_mod, cloud)
        d.init()
        d.set_property("Personal Access Token", PAT)
        d.action("CreateOAuthApp")
        redirected = cloud.authorize(d.prop("Authorization URL"))
        code = redirected.split("code=")[1]
        pasted = {"url": redirected, "raw": f"  {code}  ",
                  "httpbin_json": '{\n  "args": {\n    "code": "%s"\n  },\n  "headers": {}\n}' % code}[fmt]
        d.set_property("Authorization Code", pasted)
        assert d.prop("Authorization Status").startswith("Authorized"), (fmt, d.prop("Authorization Status"))


def test_auth_result_pasted_into_wrong_field(rt_mod):
    for field in ("OAuth Redirect URI", "Personal Access Token"):
        cloud = FakeSmartThings()
        d = Driver(rt_mod, cloud)
        d.init()
        d.set_property("Personal Access Token", PAT)
        d.action("CreateOAuthApp")
        redirected = cloud.authorize(d.prop("Authorization URL"))
        d.set_property(field, redirected)
        assert d.prop("Authorization Status").startswith("Authorized"), (field, d.prop("Authorization Status"))
        assert cloud.token_requests[-1]["redirect_uri"] == "https://httpbin.org/get", field
        assert d.prop("OAuth Redirect URI") == "https://httpbin.org/get", field
        if field == "Personal Access Token":
            assert d.prop("Personal Access Token") == ""
        assert d.prop("Device ID") == cloud.device_id and d.prop("Driver Status") == "OK", field


def test_create_app_guards(rt_mod):
    cloud = FakeSmartThings()
    d = Driver(rt_mod, cloud)
    d.init()
    d.action("CreateOAuthApp")
    assert "Personal Access Token" in d.prop("Authorization Status") and cloud.apps_created == []
    d.set_property("Personal Access Token", "wrong-token")
    d.action("CreateOAuthApp")
    assert "Apps scopes" in d.prop("Authorization Status")
    d.set_property("OAuth Client ID", "existing")
    d.set_property("Personal Access Token", PAT)
    d.action("CreateOAuthApp")
    assert "already set" in d.prop("Authorization Status") and cloud.apps_created == []


def test_pat_only_mode(rt_mod):
    d, cloud = pat_driver(rt_mod, "da_wm_wm_01011")
    assert "Personal Access Token only" in d.prop("Authorization Status")
    d.command("PAUSE")
    d.advance(10000)
    assert d.var("STATE") == "Paused"


def test_sign_out(rt_mod):
    d, cloud = configured_driver(rt_mod)
    d.set_property("Personal Access Token", "")
    d.action("SignOut")
    assert d.prop("Authorization Status") == "Not authorized" and d.prop("Driver Status") == "Signed out"
    n = len(cloud.log)
    d.advance(10 * 60 * 1000)
    assert len(cloud.log) == n and d.icon() == "unknown"


# --------------------------------------------------------------------------
# driver.xml and driver.lua agree
# --------------------------------------------------------------------------
def test_driver_xml_matches_lua(rt_mod):
    root = xml_root()
    d = Driver(rt_mod, FakeSmartThings())
    lua = (SRC / "driver.lua").read_text(encoding="utf-8")
    assert root.findtext("name") == "DirectorLink · Samsung Washer"
    assert root.findtext("creator") == "DirectorLink" and root.findtext("manufacturer") == "Samsung"
    proxies = root.find("proxies").findall("proxy")
    assert [(p.get("proxybindingid"), p.get("name"), p.text) for p in proxies] == [("5001", "Washer", "uibutton")]
    actions = [a.findtext("command") for a in root.find("config/actions")]
    assert sorted(actions) == sorted(lua_to_py(d.g.ACTIONS).keys()), actions
    commands = [c.findtext("name") for c in root.find("config/commands")]
    assert sorted(commands) == sorted(lua_to_py(d.g.COMMANDS).keys()), commands
    events = {e.findtext("name"): int(e.findtext("id")) for e in root.find("events")}
    assert events == {"Cycle Started": 1, "Cycle Paused": 2, "Cycle Finished": 3, "Error": 4,
                      "Remote Control Enabled": 5, "Remote Control Disabled": 6, "Command Failed": 7,
                      "Authorization Required": 8}
    fired = set(re.findall(r'fireEvent\("([^"]+)"\)', lua)) | set(re.findall(r'events\[#events \+ 1\] = "([^"]+)"', lua))
    fired |= {"Remote Control Enabled", "Remote Control Disabled"}
    assert fired == set(events), fired ^ set(events)
    conds = {c.findtext("name"): [i.text for i in c.find("items")] for c in root.find("config/conditionals")}
    assert conds == {"STATE": lua_to_py(d.g.STATES), "REMOTE_CONTROL": ["Enabled", "Disabled"]}
    props = set(xml_property_defaults())
    for name in re.findall(r'ON_PROPERTY_CHANGED\["([^"]+)"\]', lua) + re.findall(r'Properties\["([^"]+)"\]', lua):
        assert name in props, f"driver.lua uses property '{name}', not in driver.xml"
    for name in re.findall(r'setProp\("([^"]+)"', lua):
        assert name in props, f"driver.lua sets property '{name}', not in driver.xml"
    states = {s.get("id") for s in root.find("capabilities").iter("state")}
    assert states == {s.lower() for s in lua_to_py(d.g.STATES)} | {"pending", "unknown"}, states
    tile_icons = set(re.findall(r'icon, desc = "(\w+)"', lua)) | {s.lower() for s in lua_to_py(d.g.STATES)}
    assert tile_icons <= states, tile_icons - states


def test_brand_lines(rt_mod):
    independent = "DirectorLink is an independent project, not affiliated with Control4 or Snap One."
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    docs = (SRC / "www" / "documentation.html").read_text(encoding="utf-8")
    notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    notes = (ROOT / "docs" / "releases" / f"v{version}.md").read_text(encoding="utf-8")
    for name, text in (("README", readme), ("documentation.html", docs), ("release notes", notes), ("NOTICE", notice)):
        assert independent in text, name
        assert "Not affiliated with or endorsed by Samsung." in text, name
    for name, text in (("README", readme), ("documentation.html", docs)):
        assert "DirectorLink · Samsung Washer for Control4" in text, name
        assert "Free. No subscription, no license key, no account with us." in text, name
        assert re.search(r"Made by .*DirectorLink.*Works on its own: DirectorLink is not required\.", text), name
        assert "https://github.directorlink.io" in text and "https://directorlink.io/drivers/samsung-washer" in text, name
    assert "## Testing the beta" in readme and "Print Diagnostics" in readme.split("## Testing the beta")[1]
    # Names homeowners see carry no brand
    root = xml_root()
    seen = [p.get("name") for p in root.find("proxies")] + [c.findtext("connectionname") for c in root.find("connections")]
    lua = (SRC / "driver.lua").read_text(encoding="utf-8")
    seen += re.findall(r'desc = "(Washer:[^"]*)"', lua)
    for s in seen:
        assert "DirectorLink" not in s and "Samsung" not in s, s


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main():
    failures = 0
    for name, mod in (("Lua 5.1", lua51), ("LuaJIT 2.1", luajit21)):
        for t in TESTS:
            try:
                t(mod)
                print(f"PASS  [{name}] {t.__name__}")
            except Exception:
                failures += 1
                print(f"FAIL  [{name}] {t.__name__}")
                traceback.print_exc()
    total = len(TESTS) * 2
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
