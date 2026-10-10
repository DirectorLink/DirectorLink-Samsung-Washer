-- Minimal Control4 DriverWorks runtime mock for offline tests.
-- Virtual clock: MOCK.ms (timer time) and MOCK.now (os.time()).

MOCK = {
  start = 1790000000, now = 1790000000, ms = 0,
  timers = {}, requests = {}, events = {}, proxy = {}, persist = {},
  vars = {}, errors = {}, prints = {}, plist = {}, propsLog = {},
}

local realTime = os.time
os.time = function(t)
  if t then return realTime(t) end
  return MOCK.now
end

print = function(...)
  local parts = {}
  for i = 1, select("#", ...) do parts[#parts + 1] = tostring((select(i, ...))) end
  local line = table.concat(parts, "\t")
  MOCK.prints[#MOCK.prints + 1] = line
  if MOCK.echo then io.write(line, "\n") end
end

local function deepcopy(v)
  if type(v) ~= "table" then return v end
  local out = {}
  for k, x in pairs(v) do out[k] = deepcopy(x) end
  return out
end

C4 = {}
function C4:UpdateProperty(n, v)
  Properties[n] = tostring(v)
  MOCK.propsLog[#MOCK.propsLog + 1] = n
end
function C4:UpdatePropertyList(n, list, def)
  MOCK.plist[n] = list
  if def then Properties[n] = def end
end
function C4:SetPropertyAttribs() end
function C4:AddVariable(n, v, t, ro, hidden)
  assert(MOCK.inInit, "AddVariable must be called in OnDriverInit")
  -- Director numbers a driver's variables in the order they are added, from 1001.
  MOCK.varOrder = MOCK.varOrder or {}
  MOCK.varOrder[#MOCK.varOrder + 1] = n
  MOCK.varIds = MOCK.varIds or {}
  MOCK.varIds[n] = 1000 + #MOCK.varOrder
  MOCK.varTypes = MOCK.varTypes or {}
  MOCK.varTypes[n] = t
  MOCK.vars[n] = v
  return MOCK.varIds[n], true
end
function C4:SetVariable(n, v)
  assert(type(v) == "string", "SetVariable value must be a string")
  MOCK.varSets = MOCK.varSets or {}
  MOCK.varSets[n] = (MOCK.varSets[n] or 0) + 1
  MOCK.vars[n] = v
end
function C4:FireEvent(n) MOCK.events[#MOCK.events + 1] = n end
function C4:SendToProxy(b, cmd, p, typ)
  assert(not MOCK.inInit, "SendToProxy must not be called in OnDriverInit")
  MOCK.proxy[#MOCK.proxy + 1] = { binding = b, cmd = cmd, params = deepcopy(p) }
end
function C4:Base64Encode(s) return PY_B64(s) end
function C4:PersistSetValue(n, v, enc)
  MOCK.persist[n] = { value = deepcopy(v), enc = enc and true or false }
end
function C4:PersistGetValue(n, enc)
  local e = MOCK.persist[n]
  if not e then return nil end
  assert(e.enc == (enc and true or false), "persist encryption flag mismatch for " .. n)
  return deepcopy(e.value)
end
function C4:PersistDeleteValue(n) MOCK.persist[n] = nil end
function C4:GetDeviceID() return 1234 end
function C4:GetDriverConfigInfo(k) if k == "version" then return "1" end end
function C4:GetVersionInfo() return { version = "4.2.1.000000" } end
function C4:ErrorLog(s) MOCK.errors[#MOCK.errors + 1] = s end

function C4:SetTimer(ms, fn, rep)
  assert(not MOCK.inInit, "SetTimer must not be called in OnDriverInit")
  assert(type(ms) == "number" and ms > 0, "SetTimer delay must be > 0")
  local t = { due = MOCK.ms + ms, interval = ms, fn = fn, rep = rep and true or false, cancelled = false }
  function t:Cancel() self.cancelled = true end
  MOCK.timers[#MOCK.timers + 1] = t
  return t
end

function C4:url()
  assert(not MOCK.inInit, "C4:url must not be used in OnDriverInit")
  local o = { opts = {} }
  function o:SetOptions(op) for k, v in pairs(op) do self.opts[k] = v end return self end
  function o:OnDone(f) self.ondone = f return self end
  local function start(self, method, url, data, headers)
    assert(self.ondone, "OnDone must be set before starting the transfer")
    self.method, self.url, self.data, self.headers = method, url, data, deepcopy(headers or {})
    MOCK.requests[#MOCK.requests + 1] = self
    return self
  end
  function o:Get(u, h) return start(self, "GET", u, nil, h) end
  function o:Post(u, d, h) return start(self, "POST", u, d, h) end
  function o:Put(u, d, h) return start(self, "PUT", u, d, h) end
  function o:Delete(u, h) return start(self, "DELETE", u, nil, h) end
  function o:Custom(u, m, d, h) return start(self, m, u, d, h) end
  function o:TicketId() return 1 end
  return o
end

function MOCK.takeRequest() return table.remove(MOCK.requests, 1) end

function MOCK.respond(req, code, body, errCode, errMsg)
  if errCode and errCode ~= 0 then
    req.ondone(req, {}, errCode, errMsg)
  else
    req.ondone(req, { { code = code, headers = {}, body = body } }, 0, nil)
  end
end

-- Returns the earliest active timer due at or before limit, or nil
function MOCK.nextDue(limit)
  local best
  for _, t in ipairs(MOCK.timers) do
    if not t.cancelled and t.due <= limit and (not best or t.due < best.due) then best = t end
  end
  return best
end

function MOCK.fire(t)
  MOCK.ms = t.due
  MOCK.now = MOCK.start + math.floor(t.due / 1000)
  if t.rep then t.due = t.due + t.interval else t.cancelled = true end
  t.fn(t, 0)
end

function MOCK.setMs(ms)
  MOCK.ms = ms
  MOCK.now = MOCK.start + math.floor(ms / 1000)
end

function MOCK.lastIcon()
  for i = #MOCK.proxy, 1, -1 do
    if MOCK.proxy[i].cmd == "ICON_CHANGED" then return MOCK.proxy[i].params.icon end
  end
end

function MOCK.lastLed()
  for i = #MOCK.proxy, 1, -1 do
    if MOCK.proxy[i].cmd == "MATCH_LED_STATE" then return MOCK.proxy[i].params.STATE end
  end
end
