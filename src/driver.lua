--[[
  DirectorLink · Samsung Washer - Control4 DriverWorks driver
  Part of DirectorLink Drivers - https://directorlink.io/drivers/samsung-washer - Apache License 2.0

  Follows Samsung Wi-Fi washing machines through the SmartThings cloud REST API, and
  pauses, resumes, stops or starts them with washerOperatingState.setMachineState, the
  command Home Assistant's smartthings integration uses:

    State       samsungce.washerOperatingState (operatingState, washerJobState, remainingTime,
                progress), else washerOperatingState (machineState, washerJobState, completionTime)
    Programme   samsungce.washerCycle (Table_XX_Course_YY, named from course tables checked on
                real washers), else washerMode
    Settings    custom.washerSpinLevel, custom.washerWaterTemperature, custom.washerRinseCycles,
                samsungce.kidsLock, samsungce.washerDelayEnd, remoteControlStatus, switch
    Errors      samsungce.errorAndAlarmState / custom.error, when the washer reports them
    Energy      powerConsumptionReport

  Safety: Start and Resume set the drum moving, so they are sent only when the washer reports
  remote control enabled (Smart Control pressed on the washer) in a status read made just
  before sending. Otherwise they are refused, and never retried. The programme, temperature
  and spin are never changed. A tap on the tile only reads the washer again.

  Capabilities that are missing, or listed in custom.disabledCapabilities, are ignored.

  Auth: SmartThings OAuth2 (API_ONLY app created with a Personal Access Token).
  Access tokens last 24h; refresh tokens rotate on every use, so every refresh
  result is persisted immediately and refreshes are serialized.

  Lua 5.1 / LuaJIT compatible.
]]

------------------------------------------------------------------------------
-- Constants
------------------------------------------------------------------------------
local API_BASE        = "https://api.smartthings.com/v1"
local AUTHORIZE_URL   = "https://api.smartthings.com/oauth/authorize"
local TOKEN_URL       = "https://auth-global.api.smartthings.com/oauth/token"
local OAUTH_SCOPES    = { "r:devices:*", "x:devices:*" }
DRIVER_VERSION        = "dev"   -- scripts/build.py stamps the VERSION file here
local USER_AGENT      = "DirectorLink-Samsung-Washer/" .. DRIVER_VERSION
local DISCOVER_PROMPT = "(run Discover Washers)"
local SMART_CONTROL   = "Press Smart Control on the washer first"

local RENEW_MARGIN_S     = 4 * 3600          -- renew when less than this is left
local HOUSEKEEPING_MS    = 30 * 60 * 1000    -- token check interval
local RENEW_RETRY_MS     = 5 * 60 * 1000     -- retry after a transient renew failure
local CONFIRM_DELAYS_S   = { 3, 7, 15, 30 }  -- status re-reads after a command
local STATUS_HOLD_S      = 120               -- a command message stays in Driver Status this long
local RATE_LIMIT_PAUSE_S = 120               -- no polling for this long after HTTP 429
local STOP_GRACE_S       = 180               -- a cycle that ends this soon after Stop was cancelled
local OFFLINE_GRACE_S    = 10 * 60           -- a washer offline mid-cycle keeps its cycle this long past its finish time
local DIAG_EVENTS        = 10                -- events kept for Print Diagnostics

local TILE_BINDING = 5001                    -- the "Washer" tile (primary proxy)

local PERSIST_AUTH  = "ST_AUTH"
local PERSIST_CYCLE = "ST_CYCLE"

STATES = { "Off", "Ready", "Running", "Paused", "Finished", "Error" }
local TILE_ICON = { Off = "off", Ready = "ready", Running = "running", Paused = "paused", Finished = "finished",
  Error = "error" }

-- Course names by course table, from washers' own course lists (see NOTICE: localthings, MIT).
-- An unknown code shows as "Course 1C".
COURSE_NAMES = {
  Table_00 = {
    ["01"] = "Normal", ["55"] = "Whites", ["57"] = "Self Clean+", ["5b"] = "Cotton", ["5c"] = "Extra Speed",
    ["5d"] = "Super Eco Wash", ["5e"] = "Delicates", ["5f"] = "Baby Care", ["60"] = "Outdoor",
    ["61"] = "Dark Garment", ["63"] = "Drum Clean", ["64"] = "Rinse + Spin", ["65"] = "Wool", ["66"] = "Bedding",
    ["67"] = "Synthetics", ["68"] = "E Cotton", ["6b"] = "Spin Only", ["6c"] = "Denim", ["70"] = "Heavy Duty",
    ["71"] = "Bedding", ["72"] = "Sanitize", ["73"] = "Rinse + Spin", ["74"] = "Active Wear", ["75"] = "Delicates",
    ["77"] = "Perm Press", ["78"] = "Quick Wash", ["ba"] = "Drain/Spin", ["d0"] = "Cotton", ["d1"] = "E Cotton",
    ["d2"] = "Synthetics", ["d3"] = "Delicates", ["d4"] = "Rinse + Spin", ["d5"] = "Drum Clean", ["d6"] = "Bedding",
    ["d7"] = "Outdoor Care", ["d8"] = "Wool", ["da"] = "Super Eco Wash", ["db"] = "Super Speed",
    ["dc"] = "15' Quick Wash", ["e4"] = "Anti Allergen",
  },
  Table_02 = {
    ["01"] = "Normal", ["02"] = "Extra Heavy Duty", ["03"] = "Super Eco Wash", ["04"] = "Quick Wash",
    ["05"] = "Wool/Lingerie", ["06"] = "Bedding", ["07"] = "Outdoor", ["08"] = "Rinse+Spin", ["09"] = "Drum Clean",
    ["0a"] = "Towels", ["0b"] = "Boil Wash", ["0c"] = "Baby Care", ["0d"] = "Spin Only", ["0e"] = "Cloudy Day",
    ["0f"] = "Pure Wash", ["10"] = "Spin Dry", ["11"] = "Summer Bedding", ["12"] = "Cottons",
    ["13"] = "Black Cottons", ["14"] = "Delicate Underwear", ["15"] = "Activewear", ["16"] = "Blouses",
    ["17"] = "Downloaded", ["18"] = "Soft Bubble", ["19"] = "AI Wash", ["1a"] = "Shirts", ["1b"] = "Cotton",
    ["1c"] = "Eco 40-60", ["1d"] = "Super Speed", ["1e"] = "15' Quick Wash", ["1f"] = "Intense Cold",
    ["20"] = "Hygiene Steam", ["21"] = "Colors", ["22"] = "Wool", ["23"] = "Outdoor", ["24"] = "Bedding",
    ["25"] = "Synthetics", ["26"] = "Delicates", ["27"] = "Rinse+Spin", ["28"] = "Drain/Spin",
    ["29"] = "Drum Clean+", ["2a"] = "Jeans", ["2b"] = "AI Wash", ["2d"] = "Silent Wash", ["2e"] = "Baby Care",
    ["2f"] = "Activewear", ["30"] = "Cloudy Day", ["32"] = "Shirts", ["33"] = "Towels", ["34"] = "Mixed",
    ["35"] = "E Cotton", ["36"] = "Wash+Dry", ["37"] = "Air Wash", ["38"] = "Cotton Dry", ["39"] = "Synthetics Dry",
    ["3a"] = "Drum Clean", ["51"] = "Super Speed", ["52"] = "Eco Cold", ["53"] = "Heavy Duty", ["54"] = "Towels",
    ["55"] = "Activewear", ["56"] = "Bedding", ["57"] = "Delicates", ["58"] = "Wool", ["5a"] = "Steam Sanitize",
    ["5b"] = "Small Load", ["5c"] = "Outdoor", ["5d"] = "Power Rinse", ["5e"] = "Rinse+Spin", ["5f"] = "Spin Only",
    ["60"] = "Self Clean+", ["63"] = "Power Steam", ["64"] = "Steam Whites", ["65"] = "Colors", ["66"] = "Denim",
    ["67"] = "Steam Allergen", ["68"] = "Steam Bulky", ["69"] = "AI Wash", ["6a"] = "Wool", ["6b"] = "Denim",
    ["6c"] = "Blouses", ["6d"] = "Delicates", ["6e"] = "Active Wear", ["6f"] = "Bedding", ["70"] = "Towels",
    ["71"] = "Quick Wash", ["72"] = "Shirts", ["73"] = "Sanitize", ["74"] = "Drum Clean", ["75"] = "Outdoor",
    ["76"] = "Baby Care", ["77"] = "Cottons", ["78"] = "Rinse + Spin", ["79"] = "Spin Only", ["7c"] = "Whites",
    ["7d"] = "Bedding/Waterproof", ["7e"] = "Self-Clean", ["7f"] = "Wool/Delicate", ["85"] = "Steam Normal",
    ["86"] = "Deep Wash", ["87"] = "Download", ["88"] = "Pet Care", ["8b"] = "Quick Wash", ["8c"] = "AI OptiWash",
    ["8f"] = "Intense Cold", ["96"] = "Less Microfiber", ["a0"] = "15' Quick Wash", ["b0"] = "Mixed Load",
  },
}

local JOB_NAMES = {
  wash = "Washing", rinse = "Rinsing", spin = "Spinning", preWash = "Pre-wash", prewash = "Pre-wash",
  drying = "Drying", weightSensing = "Weighing", delayWash = "Delay Start", aIWash = "Washing",
  aIRinse = "Rinsing", aISpin = "Spinning", aIDrying = "Drying", airWash = "Air Wash", airwash = "Air Wash",
  cooling = "Cooling", wrinklePrevent = "Wrinkle Prevent", freezeProtection = "Freeze Protection",
  soaking = "Soaking", preDrain = "Draining", drain = "Draining", finish = "Finished",
}
local SPIN_NAMES = { rinseHold = "Rinse Hold", noSpin = "No Spin", extraHigh = "Extra High", none = "None" }
local TEMPERATURE_NAMES = { tapCold = "Tap Cold", extraHot = "Extra Hot", none = "None" }
local NO_ERROR = { [""] = true, none = true, None = true, noError = true, normal = true, ["0"] = true }

------------------------------------------------------------------------------
-- Runtime state
------------------------------------------------------------------------------
gDebug = false
gAuth = nil          -- { access_token, refresh_token, expires_at, client_id, scope, installed_app_id }
gDevices = {}        -- list label -> deviceId (from discovery)
gState = {
  online = nil,      -- true | false | nil (SmartThings health)
  authProblem = nil, -- string when re-authorization is required
  res = nil,         -- last parsed status (ParseDeviceStatus)
  lastStatus = nil,  -- last /status answer, for Print Diagnostics (never printed whole)
  lastRead = nil,    -- os.time() of the last status read
  readFailures = 0,
  lastReadError = nil,
  refreshing = false,  -- tile tapped, refresh in progress
  refreshError = nil,  -- last status refresh error
  pending = nil,       -- { label, verb, seq } while a machine command is being confirmed
  holdUntil = nil,     -- Driver Status keeps a command message until then
  rateLimitedUntil = nil,
  keptOffline = false, -- the washer dropped offline mid-cycle; its last state is shown
}
-- What the driver knows about the current cycle. Persisted, so a controller restart neither
-- repeats an event nor misses a cycle that finished meanwhile.
local function newCycle()
  return {
    deviceId = nil,
    state = nil,          -- last STATE; nil before the first read
    inCycle = false,      -- a cycle that started has not ended yet
    lastRemaining = nil,  -- minutes left at the last Running/Paused read
    lastRunningAt = nil,
    expectedFinishAt = nil,
    finishedAt = nil,     -- when Finished started (nil when not showing Finished)
    offSinceFinish = false,
    lastFinished = "",    -- LAST_FINISHED, ISO 8601 UTC
    remote = nil,         -- last REMOTE_CONTROL_ENABLED
    stopAt = nil,         -- when a Stop command was accepted
  }
end
gCycle = newCycle()
gOps = {}            -- pending command confirmations, by key
gEventLog = {}       -- last events, for Print Diagnostics
local gSeq = 0
local gTimers = {}
local gRenewing = false
local gRenewWaiters = {}
local gPollMs = nil
local gPersistedCycle = nil
local gPublished = {}

------------------------------------------------------------------------------
-- Logging
------------------------------------------------------------------------------
local function log(...)
  local parts = {}
  for i = 1, select("#", ...) do parts[#parts + 1] = tostring((select(i, ...))) end
  print(os.date("%Y-%m-%d %H:%M:%S") .. " [SamsungWasher] " .. table.concat(parts, " "))
end

local function dbg(...)
  if gDebug then log(...) end
end

local function logError(msg)
  log("ERROR: " .. tostring(msg))
  if C4.ErrorLog then pcall(C4.ErrorLog, C4, "[DirectorLink Samsung Washer] " .. tostring(msg)) end
end

------------------------------------------------------------------------------
-- Minimal JSON codec (C4:JsonEncode turns Lua arrays into objects by default
-- and C4:JsonDecode turns null into {}, so we bundle our own).
-- null decodes to nil.
------------------------------------------------------------------------------
JSON = {}

do
  local escapes = {
    ['"'] = '\\"', ['\\'] = '\\\\', ['\b'] = '\\b', ['\f'] = '\\f',
    ['\n'] = '\\n', ['\r'] = '\\r', ['\t'] = '\\t',
  }

  local function encodeString(s)
    return '"' .. (s:gsub('[%c"\\]', function(c)
      return escapes[c] or string.format("\\u%04x", c:byte())
    end)) .. '"'
  end

  local function isArray(t)
    local n = 0
    for k in pairs(t) do
      if type(k) ~= "number" or k < 1 or math.floor(k) ~= k then return false end
      if k > n then n = k end
    end
    return n > 0 and n == #t, n
  end

  local encodeValue

  function encodeValue(v, out)
    local tv = type(v)
    if v == nil then
      out[#out + 1] = "null"
    elseif tv == "boolean" then
      out[#out + 1] = v and "true" or "false"
    elseif tv == "number" then
      if v ~= v or v == math.huge or v == -math.huge then error("cannot encode non-finite number") end
      if math.floor(v) == v and math.abs(v) < 1e15 then
        out[#out + 1] = string.format("%d", v)
      else
        out[#out + 1] = string.format("%.17g", v)
      end
    elseif tv == "string" then
      out[#out + 1] = encodeString(v)
    elseif tv == "table" then
      local arr, n = isArray(v)
      if arr then
        out[#out + 1] = "["
        for i = 1, n do
          if i > 1 then out[#out + 1] = "," end
          encodeValue(v[i], out)
        end
        out[#out + 1] = "]"
      else
        local keys = {}
        for k in pairs(v) do keys[#keys + 1] = tostring(k) end
        table.sort(keys)
        out[#out + 1] = "{"
        for i, k in ipairs(keys) do
          if i > 1 then out[#out + 1] = "," end
          out[#out + 1] = encodeString(k)
          out[#out + 1] = ":"
          local val = v[k]
          if val == nil then val = v[tonumber(k)] end
          encodeValue(val, out)
        end
        out[#out + 1] = "}"
      end
    else
      error("cannot encode type " .. tv)
    end
  end

  function JSON.encode(v)
    local out = {}
    encodeValue(v, out)
    return table.concat(out)
  end

  local function utf8char(cp)
    if cp < 0x80 then
      return string.char(cp)
    elseif cp < 0x800 then
      return string.char(0xC0 + math.floor(cp / 0x40), 0x80 + cp % 0x40)
    elseif cp < 0x10000 then
      return string.char(0xE0 + math.floor(cp / 0x1000), 0x80 + math.floor(cp / 0x40) % 0x40, 0x80 + cp % 0x40)
    else
      return string.char(0xF0 + math.floor(cp / 0x40000), 0x80 + math.floor(cp / 0x1000) % 0x40,
        0x80 + math.floor(cp / 0x40) % 0x40, 0x80 + cp % 0x40)
    end
  end

  local simpleEscapes = { b = "\b", f = "\f", n = "\n", r = "\r", t = "\t", ['"'] = '"', ["\\"] = "\\", ["/"] = "/" }

  local function decodeError(pos, msg)
    error(string.format("JSON decode error at position %d: %s", pos, msg), 0)
  end

  local function skip(str, pos)
    return str:find("[^ \t\r\n]", pos) or (#str + 1)
  end

  local decodeValue

  local function decodeString(str, pos)
    local parts = {}
    local i = pos + 1
    while true do
      local s = str:find('["\\]', i)
      if not s then decodeError(pos, "unterminated string") end
      parts[#parts + 1] = str:sub(i, s - 1)
      if str:sub(s, s) == '"' then
        return table.concat(parts), s + 1
      end
      local esc = str:sub(s + 1, s + 1)
      if esc == "u" then
        local hex = str:sub(s + 2, s + 5)
        if not hex:match("^%x%x%x%x$") then decodeError(s, "bad unicode escape") end
        local cp = tonumber(hex, 16)
        local nextI = s + 6
        if cp >= 0xD800 and cp <= 0xDBFF and str:sub(nextI, nextI + 1) == "\\u" then
          local lo = tonumber(str:sub(nextI + 2, nextI + 5), 16)
          if lo and lo >= 0xDC00 and lo <= 0xDFFF then
            cp = 0x10000 + (cp - 0xD800) * 0x400 + (lo - 0xDC00)
            nextI = nextI + 6
          end
        end
        parts[#parts + 1] = utf8char(cp)
        i = nextI
      else
        local c = simpleEscapes[esc]
        if not c then decodeError(s, "bad escape") end
        parts[#parts + 1] = c
        i = s + 2
      end
    end
  end

  function decodeValue(str, pos)
    pos = skip(str, pos)
    local c = str:sub(pos, pos)
    if c == "{" then
      local obj = {}
      pos = skip(str, pos + 1)
      if str:sub(pos, pos) == "}" then return obj, pos + 1 end
      while true do
        pos = skip(str, pos)
        if str:sub(pos, pos) ~= '"' then decodeError(pos, "expected string key") end
        local key
        key, pos = decodeString(str, pos)
        pos = skip(str, pos)
        if str:sub(pos, pos) ~= ":" then decodeError(pos, "expected ':'") end
        local val
        val, pos = decodeValue(str, pos + 1)
        obj[key] = val
        pos = skip(str, pos)
        local d = str:sub(pos, pos)
        if d == "}" then return obj, pos + 1 end
        if d ~= "," then decodeError(pos, "expected ',' or '}'") end
        pos = pos + 1
      end
    elseif c == "[" then
      local arr, n = {}, 0
      pos = skip(str, pos + 1)
      if str:sub(pos, pos) == "]" then return arr, pos + 1 end
      while true do
        local val
        val, pos = decodeValue(str, pos)
        n = n + 1
        arr[n] = val
        pos = skip(str, pos)
        local d = str:sub(pos, pos)
        if d == "]" then return arr, pos + 1 end
        if d ~= "," then decodeError(pos, "expected ',' or ']'") end
        pos = pos + 1
      end
    elseif c == '"' then
      return decodeString(str, pos)
    elseif c == "-" or c:match("%d") then
      local num = str:match("^-?%d+%.?%d*[eE]?[-+]?%d*", pos)
      local v = tonumber(num)
      if not v then decodeError(pos, "bad number") end
      return v, pos + #num
    elseif str:sub(pos, pos + 3) == "true" then
      return true, pos + 4
    elseif str:sub(pos, pos + 4) == "false" then
      return false, pos + 5
    elseif str:sub(pos, pos + 3) == "null" then
      return nil, pos + 4
    end
    decodeError(pos, "unexpected character '" .. c .. "'")
  end

  function JSON.decode(str)
    if type(str) ~= "string" then error("JSON.decode expects a string", 0) end
    local v, pos = decodeValue(str, 1)
    pos = skip(str, pos)
    if pos <= #str then decodeError(pos, "trailing garbage") end
    return v
  end
end

------------------------------------------------------------------------------
-- Small helpers
------------------------------------------------------------------------------
local function trim(s)
  return (tostring(s or ""):gsub("^%s+", ""):gsub("%s+$", ""))
end

local function urlEncode(s)
  return (tostring(s):gsub("[^%w%-%._~]", function(c) return string.format("%%%02X", c:byte()) end))
end

local function urlDecode(s)
  s = tostring(s):gsub("%+", " ")
  return (s:gsub("%%(%x%x)", function(h) return string.char(tonumber(h, 16)) end))
end

-- pairsList is a list of {key, value} so the output order is stable
local function formEncode(pairsList)
  local out = {}
  for _, kv in ipairs(pairsList) do
    out[#out + 1] = urlEncode(kv[1]) .. "=" .. urlEncode(kv[2])
  end
  return table.concat(out, "&")
end

local function dig(t, ...)
  for i = 1, select("#", ...) do
    if type(t) ~= "table" then return nil end
    t = t[(select(i, ...))]
  end
  return t
end

local function listHas(list, value)
  if type(list) ~= "table" then return false end
  for _, v in ipairs(list) do
    if v == value then return true end
  end
  return false
end

local function fmtTime(ts)
  return os.date("%Y-%m-%d %H:%M", ts)
end

local function fmtNumber(v)
  if type(v) ~= "number" then return tostring(v) end
  if math.floor(v) == v then return string.format("%d", v) end
  return string.format("%.1f", v)
end

local function fmtMinutes(m)
  m = math.max(0, math.floor((tonumber(m) or 0) + 0.5))
  if m >= 60 then return string.format("%d h %d min", math.floor(m / 60), m % 60) end
  return m .. " min"
end

local function randomHex(n)
  local out = {}
  for i = 1, n do out[i] = string.format("%x", math.random(0, 15)) end
  return table.concat(out)
end

-- "extraHigh" -> "Extra High"
local function words(s)
  s = tostring(s):gsub("(%l)(%u)", "%1 %2"):gsub("_", " ")
  return (s:gsub("(%a)([%w]*)", function(a, b) return a:upper() .. b end))
end

-- Days since 1970-01-01 for a proleptic Gregorian date (no time zone involved).
local function daysFromCivil(y, m, d)
  if m <= 2 then y = y - 1 end
  local era = math.floor(y / 400)
  local yoe = y - era * 400
  local mp = (m + 9) % 12
  local doy = math.floor((153 * mp + 2) / 5) + d - 1
  local doe = yoe * 365 + math.floor(yoe / 4) - math.floor(yoe / 100) + doy
  return era * 146097 + doe - 719468
end

-- "2025-04-25T10:34:12Z" (or with .fff, +hh:mm) -> seconds since the epoch
function ParseIsoUtc(s)
  if type(s) ~= "string" then return nil end
  local y, mo, d, h, mi, sec, rest = s:match("^(%d%d%d%d)%-(%d%d)%-(%d%d)T(%d%d):(%d%d):(%d%d)(.*)$")
  if not y then return nil end
  local ts = daysFromCivil(tonumber(y), tonumber(mo), tonumber(d)) * 86400
    + tonumber(h) * 3600 + tonumber(mi) * 60 + tonumber(sec)
  rest = rest:gsub("^%.%d+", "")
  local sign, oh, om = rest:match("^([+-])(%d%d):?(%d%d)$")
  if sign then
    local off = tonumber(oh) * 3600 + tonumber(om) * 60
    ts = (sign == "+") and (ts - off) or (ts + off)
  end
  return ts
end

local function isoUtc(ts)
  return os.date("!%Y-%m-%dT%H:%M:%SZ", ts)
end

local function setProp(name, value)
  value = tostring(value == nil and "" or value)
  if Properties[name] ~= value then C4:UpdateProperty(name, value) end
end

-- Variables are set only when their value changes.
local function setVar(name, value)
  value = tostring(value == nil and "" or value)
  if gPublished[name] == value then return end
  gPublished[name] = value
  pcall(C4.SetVariable, C4, name, value)
end

local function fireEvent(name)
  dbg("Firing event:", name)
  table.insert(gEventLog, 1, os.date("%Y-%m-%d %H:%M:%S") .. " " .. name)
  while #gEventLog > DIAG_EVENTS do table.remove(gEventLog) end
  pcall(C4.FireEvent, C4, name)
end

local function cancelTimer(name)
  local t = gTimers[name]
  if t then
    pcall(function() t:Cancel() end)
    gTimers[name] = nil
  end
end

local function setTimer(name, ms, fn, rep)
  cancelTimer(name)
  gTimers[name] = C4:SetTimer(ms, function(timer, skips)
    if not rep then gTimers[name] = nil end
    local ok, err = pcall(fn, timer, skips)
    if not ok then logError("timer " .. name .. ": " .. tostring(err)) end
  end, rep and true or false)
end

local function persistGet(name, encrypted)
  local ok, v = pcall(C4.PersistGetValue, C4, name, encrypted)
  if ok then return v end
  return nil
end

local function persistSet(name, value, encrypted)
  local ok, err = pcall(C4.PersistSetValue, C4, name, value, encrypted)
  if not ok then logError("PersistSetValue(" .. name .. ") failed: " .. tostring(err)) end
end

local function persistDelete(name)
  if C4.PersistDeleteValue then pcall(C4.PersistDeleteValue, C4, name) end
end

local function persistCycle()
  gCycle.deviceId = trim(Properties["Device ID"])
  local s = JSON.encode(gCycle)
  if s ~= gPersistedCycle then
    gPersistedCycle = s
    persistSet(PERSIST_CYCLE, gCycle, false)
  end
end

------------------------------------------------------------------------------
-- Presentation (tile, properties)
------------------------------------------------------------------------------
local function setDriverStatus(text, holdSeconds)
  setProp("Driver Status", text)
  gState.holdUntil = holdSeconds and (os.time() + holdSeconds) or nil
end

function deviceId()
  return trim(Properties["Device ID"])
end

-- The "Washer" tile: the washer's state at a glance. A tap only reads the washer again.
function UpdateTile()
  local icon, desc
  local st = gCycle.state
  if gState.pending then
    icon, desc = "pending", "Washer: " .. gState.pending.verb .. "..."
  elseif gState.refreshing then
    icon, desc = "pending", "Washer: refreshing..."
  elseif gState.authProblem then
    icon, desc = "error", "Washer: authorization required"
  elseif not bearerToken() or deviceId() == "" then
    icon, desc = "unknown", "Washer: not set up"
  elseif not st then
    if gState.refreshError then
      icon, desc = "error", "Washer: " .. gState.refreshError
    else
      icon, desc = "unknown", "Washer: waiting for status"
    end
  else
    icon = TILE_ICON[st] or "unknown"
    local job, prog = Properties["Job"] or "", Properties["Programme"] or ""
    local left, finish = Properties["Time Remaining"] or "", Properties["Finish Time"] or ""
    local parts = {}
    if st == "Running" then
      desc = "Washer: " .. ((job ~= "" and job ~= "-") and job or "Running")
      if prog ~= "" and prog ~= "Not available" then parts[#parts + 1] = prog end
      if left ~= "" then parts[#parts + 1] = left .. " left" end
      if finish ~= "" then parts[#parts + 1] = "done " .. finish end
    elseif st == "Paused" then
      desc = "Washer: Paused"
      if prog ~= "" and prog ~= "Not available" then parts[#parts + 1] = prog end
      if left ~= "" then parts[#parts + 1] = left .. " left" end
    elseif st == "Finished" then
      desc = "Washer: Finished" .. (finish ~= "" and (" at " .. finish) or "")
    elseif st == "Error" then
      desc = "Washer: Error" .. ((gState.res and #gState.res.errors > 0) and (" " .. table.concat(gState.res.errors, ", ")) or "")
    elseif st == "Ready" then
      desc = "Washer: Ready"
      if prog ~= "" and prog ~= "Not available" then parts[#parts + 1] = prog end
    else
      desc = "Washer: Off"
    end
    if #parts > 0 then desc = desc .. " - " .. table.concat(parts, ", ") end
    if gState.keptOffline then desc = desc .. " (offline, last known)" end
    if gState.refreshError then desc = desc .. " (" .. gState.refreshError .. ")" end
  end
  if icon ~= gState.tileIcon or desc ~= gState.tileDesc then
    pcall(C4.SendToProxy, C4, TILE_BINDING, "ICON_CHANGED", { icon = icon, icon_description = desc })
    gState.tileIcon, gState.tileDesc = icon, desc
  end
end

local function updateAuthStatus()
  local text
  if gState.authProblem then
    text = "Re-authorization required: " .. gState.authProblem
  elseif gAuth and gAuth.access_token then
    if gAuth.refresh_token then
      text = "Authorized (OAuth) - access token valid until " .. fmtTime(gAuth.expires_at or 0) ..
        ", renews automatically"
    else
      text = "Authorized (no refresh token) - expires " .. fmtTime(gAuth.expires_at or 0)
    end
  elseif trim(Properties["Personal Access Token"]) ~= "" then
    text = "Using Personal Access Token only (new PATs expire after 24h - finish OAuth setup)"
  else
    text = "Not authorized"
  end
  setProp("Authorization Status", text)
end

local function commandFailed(msg)
  logError(msg)
  setDriverStatus(msg, STATUS_HOLD_S)
  fireEvent("Command Failed")
  UpdateTile()
end

------------------------------------------------------------------------------
-- HTTP
------------------------------------------------------------------------------
-- cb(errCode, errMsg, httpCode, body)  errCode 0 = transfer completed
-- sensitive = true keeps the response body (tokens, secrets) out of the log
local function httpRequest(method, url, data, headers, cb, sensitive)
  dbg(">>", method, url, data and not sensitive and data or "")
  local ok, err = pcall(function()
    local t = C4:url()
    t:SetOptions({ fail_on_error = false, timeout = 30, connect_timeout = 10 })
    t:OnDone(function(transfer, responses, errCode, errMsg)
      local r = responses and responses[#responses]
      local code = r and r.code
      local body = r and r.body or ""
      dbg("<<", method, url, "err=" .. tostring(errCode), "http=" .. tostring(code),
        (gDebug and not sensitive and #body > 0) and body:sub(1, 600) or "")
      local cbOk, cbErr = pcall(cb, errCode or -2, errMsg, code, body)
      if not cbOk then logError("HTTP callback failed: " .. tostring(cbErr)) end
    end)
    if method == "GET" then
      t:Get(url, headers)
    elseif method == "POST" then
      t:Post(url, data or "", headers)
    elseif method == "PUT" then
      t:Put(url, data or "", headers)
    elseif method == "DELETE" then
      t:Delete(url, headers)
    else
      t:Custom(url, method, data or "", headers)
    end
  end)
  if not ok then
    logError("HTTP request could not be started: " .. tostring(err))
    pcall(cb, -3, tostring(err), nil, "")
  end
end

local function safeDecode(body)
  if not body or body == "" then return nil end
  local ok, v = pcall(JSON.decode, body)
  if ok then return v end
  dbg("Response is not JSON:", body:sub(1, 200))
  return nil
end

local function describeHttpError(code, decoded)
  local msg = dig(decoded, "error", "message") or dig(decoded, "error_description")
    or dig(decoded, "message") or (type(dig(decoded, "error")) == "string" and decoded.error)
  local base = ({
    [400] = "Bad request", [401] = "Not authorized", [403] = "Forbidden (missing permission/scope)",
    [404] = "Not found (check Device ID)", [409] = "Conflict", [422] = "Rejected by the washer",
    [429] = "Rate limited by SmartThings", [500] = "SmartThings server error",
    [503] = "SmartThings unavailable",
  })[code] or ("HTTP " .. tostring(code))
  if msg and msg ~= "" then return base .. ": " .. tostring(msg) end
  return base
end

------------------------------------------------------------------------------
-- OAuth
------------------------------------------------------------------------------
local function oauthClient()
  return trim(Properties["OAuth Client ID"]), trim(Properties["OAuth Client Secret"]),
    trim(Properties["OAuth Redirect URI"])
end

local function saveTokens(resp, clientId)
  local refresh = resp.refresh_token
  if (refresh == nil or refresh == "") and gAuth then refresh = gAuth.refresh_token end
  gAuth = {
    access_token = resp.access_token,
    refresh_token = refresh,
    expires_at = os.time() + (tonumber(resp.expires_in) or 86400),
    client_id = clientId,
    scope = resp.scope,
    installed_app_id = resp.installed_app_id,
  }
  -- Persist immediately: the previous refresh token is now consumed.
  persistSet(PERSIST_AUTH, gAuth, true)
  gState.authProblem = nil
  updateAuthStatus()
end

local function clearTokens(reason)
  gAuth = nil
  persistDelete(PERSIST_AUTH)
  persistSet(PERSIST_AUTH, {}, true)
  if reason then
    gState.authProblem = reason
    fireEvent("Authorization Required")
  end
  updateAuthStatus()
  UpdateTile()
end

local function tokenRequest(bodyPairs, cb)
  local clientId, secret = oauthClient()
  if clientId == "" or secret == "" then
    cb(false, "OAuth Client ID / Secret not set", false)
    return
  end
  local headers = {
    ["Authorization"] = "Basic " .. C4:Base64Encode(clientId .. ":" .. secret),
    ["Content-Type"] = "application/x-www-form-urlencoded",
    ["Accept"] = "application/json",
    ["User-Agent"] = USER_AGENT,
  }
  httpRequest("POST", TOKEN_URL, formEncode(bodyPairs), headers, function(errCode, errMsg, code, body)
    if errCode ~= 0 or not code then
      cb(false, "Network error: " .. tostring(errMsg), true)
      return
    end
    local decoded = safeDecode(body)
    if code == 200 and type(decoded) == "table" and decoded.access_token then
      saveTokens(decoded, clientId)
      cb(true)
    else
      local transient = (code >= 500 or code == 429)
      cb(false, describeHttpError(code, decoded), transient)
    end
  end, true)
end

local function finishRenew(ok, err)
  gRenewing = false
  local waiters = gRenewWaiters
  gRenewWaiters = {}
  for _, w in ipairs(waiters) do pcall(w, ok, err) end
end

-- Renew the access token. Concurrent callers share one request, because a
-- refresh token can only be used once.
function RenewToken(cb)
  if cb then gRenewWaiters[#gRenewWaiters + 1] = cb end
  if gRenewing then return end
  if not (gAuth and gAuth.refresh_token) then
    if not gState.authProblem then
      setProp("Authorization Status", "Cannot renew - not authorized with OAuth yet")
    end
    finishRenew(false, "No refresh token - authorize first")
    return
  end
  gRenewing = true
  local clientId = oauthClient()
  log("Renewing SmartThings access token")
  tokenRequest({
    { "grant_type", "refresh_token" },
    { "client_id", clientId },
    { "refresh_token", gAuth.refresh_token },
  }, function(ok, err, transient)
    if ok then
      log("Access token renewed, valid until " .. fmtTime(gAuth.expires_at))
      cancelTimer("renewRetry")
    elseif transient then
      logError("Token renewal failed (will retry): " .. tostring(err))
      setProp("Authorization Status", "Token renewal failed, retrying: " .. tostring(err))
      setTimer("renewRetry", RENEW_RETRY_MS, function() RenewToken() end)
    else
      logError("Token renewal rejected: " .. tostring(err))
      clearTokens("token renewal rejected (" .. tostring(err) .. ")")
    end
    finishRenew(ok, err)
  end)
end

local function maybeRenewToken(cb)
  if gAuth and gAuth.refresh_token and (gAuth.expires_at or 0) - os.time() < RENEW_MARGIN_S then
    RenewToken(cb)
  elseif cb then
    cb(true)
  end
end

local function extractAuthCode(text)
  text = trim(text)
  local code = text:match("[?&]code=([^&#%s\"']+)") or text:match("^code=([^&#%s\"']+)")
    or text:match('"code"%s*:%s*"([^"]+)"')
  if not code and not text:find("[%s/?&=]") then code = text end
  return code and urlDecode(code) or nil
end

local function buildAuthUrl()
  local clientId, _, redirect = oauthClient()
  if clientId == "" or redirect == "" then return "" end
  return AUTHORIZE_URL .. "?client_id=" .. urlEncode(clientId) .. "&response_type=code" ..
    "&redirect_uri=" .. urlEncode(redirect) .. "&scope=" .. urlEncode(table.concat(OAUTH_SCOPES, " "))
end

local function updateAuthUrl()
  setProp("Authorization URL", buildAuthUrl())
end

------------------------------------------------------------------------------
-- SmartThings API
------------------------------------------------------------------------------
function bearerToken()
  if gAuth and gAuth.access_token then return gAuth.access_token, "oauth" end
  local pat = trim(Properties["Personal Access Token"])
  if pat ~= "" then return pat, "pat" end
  return nil
end

-- pathOrUrl: "/devices/..." or an absolute https URL (pagination links)
-- cb(ok, httpCode, decodedBody, errorText)
local function apiRequest(method, pathOrUrl, body, cb, isRetry)
  local token, kind = bearerToken()
  if not token then
    cb(false, nil, nil, "Not authorized - complete the SmartThings setup")
    return
  end
  if kind == "oauth" and not isRetry and gAuth.refresh_token and os.time() >= (gAuth.expires_at or 0) - 60 then
    RenewToken(function(ok, err)
      if ok then apiRequest(method, pathOrUrl, body, cb, true) else cb(false, 401, nil, err) end
    end)
    return
  end
  local headers = {
    ["Authorization"] = "Bearer " .. token,
    ["Accept"] = "application/json",
    ["User-Agent"] = USER_AGENT,
  }
  local data
  if body ~= nil then
    data = JSON.encode(body)
    headers["Content-Type"] = "application/json"
  end
  local url = pathOrUrl:find("^https://") and pathOrUrl or (API_BASE .. pathOrUrl)
  httpRequest(method, url, data, headers, function(errCode, errMsg, code, respBody)
    if errCode ~= 0 or not code then
      cb(false, nil, nil, "Network error: " .. tostring(errMsg))
      return
    end
    if code == 401 and kind == "oauth" and not isRetry then
      if gAuth and gAuth.access_token ~= token then
        apiRequest(method, pathOrUrl, body, cb, true)     -- token was renewed meanwhile
      elseif gAuth and gAuth.refresh_token then
        RenewToken(function(ok, err)
          if ok then apiRequest(method, pathOrUrl, body, cb, true) else cb(false, 401, nil, err) end
        end)
      else
        cb(false, 401, nil, "Not authorized")
      end
      return
    end
    local decoded = safeDecode(respBody)
    if code >= 200 and code < 300 then
      cb(true, code, decoded)
    else
      cb(false, code, decoded, describeHttpError(code, decoded))
    end
  end)
end

local function devicePath(suffix)
  return "/devices/" .. urlEncode(deviceId()) .. suffix
end

-- cb(ok, httpCode, errorText)
local function sendCommand(comp, cap, cmd, args, cb)
  local c = { component = comp, capability = cap, command = cmd }
  if args and #args > 0 then c.arguments = args end
  log("Command: " .. comp .. " " .. cap .. "." .. cmd .. (args and #args > 0 and ("(" .. tostring(args[1]) .. ")") or ""))
  apiRequest("POST", devicePath("/commands"), { commands = { c } }, function(ok, code, data, err)
    if ok and dig(data, "results", 1, "status") == "FAILED" then
      ok, err = false, "SmartThings reported the command as FAILED"
    end
    cb(ok, code, err)
  end)
end

------------------------------------------------------------------------------
-- Status parsing (pure function: SmartThings /status JSON -> summary)
------------------------------------------------------------------------------
local function capabilityAvailable(comps, comp, cap)
  if type(comps[comp]) ~= "table" then return false, "component not present" end
  if type(comps[comp][cap]) ~= "table" then return false, "not reported by this washer" end
  if listHas(dig(comps, comp, "custom.disabledCapabilities", "disabledCapabilities", "value"), cap) then
    return false, "disabled for this model"
  end
  return true
end

local function isOn(v)
  return v == true or v == "on" or v == "true" or v == "True" or v == "locked" or v == "enabled"
end

local function describeError(e)
  if type(e) == "string" or type(e) == "number" then return tostring(e) end
  if type(e) == "table" then
    for _, k in ipairs({ "code", "errorCode", "eventId", "id", "name", "type" }) do
      if e[k] ~= nil and type(e[k]) ~= "table" then return tostring(e[k]) end
    end
    local ok, s = pcall(JSON.encode, e)
    return ok and s:sub(1, 80) or "error"
  end
  return tostring(e)
end

-- An error entry that says it is over (status off / cleared) does not count.
local function errorActive(e)
  if type(e) ~= "table" then return not NO_ERROR[tostring(e)] end
  for _, k in ipairs({ "status", "state", "active" }) do
    local v = e[k]
    if v == false or v == "off" or v == "cleared" or v == "inactive" or v == "false" then return false end
  end
  return true
end

local function programmeName(cycle, refTable)
  if type(cycle) ~= "string" or cycle == "" then return nil end
  local tbl, code = cycle:match("^(Table_%w+)_Course_(%w+)$")
  if not tbl then
    code = cycle:match("Course_(%w+)$")
    tbl = type(refTable) == "table" and refTable.id or nil
  end
  if not code then return words(cycle) end
  local names = COURSE_NAMES[tbl or ""]
  return (names and names[code:lower()]) or ("Course " .. code:upper())
end

local function spinText(v)
  if type(v) ~= "string" and type(v) ~= "number" then return nil end
  v = tostring(v)
  if v:match("^%d+$") then return v .. " rpm" end
  return SPIN_NAMES[v] or words(v)
end

local function temperatureText(v)
  if type(v) ~= "string" and type(v) ~= "number" then return nil end
  v = tostring(v)
  if v:match("^%d+$") then return v .. " \194\176C" end
  return TEMPERATURE_NAMES[v] or words(v)
end

local function jobText(job)
  if type(job) ~= "string" or job == "" or job == "none" or job == "unknown" then return "" end
  return JOB_NAMES[job] or words(job)
end

function ParseDeviceStatus(st)
  local comps = dig(st, "components") or {}
  local main = comps.main or {}
  local res = { errors = {}, features = {} }
  local function has(cap) return (capabilityAvailable(comps, "main", cap)) end
  local function val(cap, attr)
    if not has(cap) then return nil end
    return dig(main, cap, attr, "value")
  end

  -- Operating state: Samsung's own capability first, the standard one as fallback.
  local ce = val("samsungce.washerOperatingState", "operatingState")
  local machine = val("washerOperatingState", "machineState")
  if ce == "running" or ce == "paused" or ce == "ready" then
    res.operating = ce
  elseif machine == "run" then
    res.operating = "running"
  elseif machine == "pause" then
    res.operating = "paused"
  elseif machine == "stop" then
    res.operating = "ready"
  end
  if ce == "error" or machine == "error" then res.errors[#res.errors + 1] = "error" end
  res.job = val("samsungce.washerOperatingState", "washerJobState")
  if res.job == nil or res.job == "none" then
    res.job = val("washerOperatingState", "washerJobState") or res.job
  end
  res.remaining = tonumber(val("samsungce.washerOperatingState", "remainingTime"))
  res.progress = tonumber(val("samsungce.washerOperatingState", "progress"))
  res.completion = ParseIsoUtc(val("washerOperatingState", "completionTime"))

  -- Control: washerOperatingState.setMachineState
  local controlOk, why = capabilityAvailable(comps, "main", "washerOperatingState")
  res.control = controlOk
  if not controlOk then res.controlReason = "washerOperatingState " .. why end
  local supported = val("washerOperatingState", "supportedMachineStates")
  if type(supported) == "table" and #supported > 0 then res.supportedMachineStates = supported end

  local sw = val("switch", "switch")
  if sw == "on" or sw == "off" then res.switch = sw end

  if has("samsungce.washerCycle") then
    res.programmeCode = val("samsungce.washerCycle", "washerCycle")
    res.programme = programmeName(res.programmeCode, val("samsungce.washerCycle", "referenceTable"))
  end
  if not res.programme and has("washerMode") then
    local m = val("washerMode", "washerMode")
    if type(m) == "string" and m ~= "" then res.programmeCode, res.programme = m, words(m) end
  end
  res.spin = spinText(val("custom.washerSpinLevel", "washerSpinLevel"))
  res.temperature = temperatureText(val("custom.washerWaterTemperature", "washerWaterTemperature"))
  local rinses = val("custom.washerRinseCycles", "washerRinseCycles")
  if rinses ~= nil and type(rinses) ~= "table" then res.rinses = tostring(rinses) end
  local lock = val("samsungce.kidsLock", "lockState")
  if lock ~= nil then res.kidsLock = isOn(lock) end
  local remote = val("remoteControlStatus", "remoteControlEnabled")
  if remote ~= nil then res.remote = isOn(remote) end
  local delay = val("samsungce.washerDelayEnd", "remainingTime")
  if delay ~= nil then res.delayEnd = tonumber(delay) or 0 end

  if has("samsungce.errorAndAlarmState") then
    res.errorReports = true
    local ev = val("samsungce.errorAndAlarmState", "events")
    if type(ev) == "table" then
      for _, e in ipairs(ev) do
        if errorActive(e) then res.errors[#res.errors + 1] = describeError(e) end
      end
    end
  end
  if has("custom.error") then
    res.errorReports = true
    local e = val("custom.error", "error")
    if e ~= nil and errorActive(e) and not (type(e) == "table" and next(e) == nil) then
      res.errors[#res.errors + 1] = describeError(e)
    end
  end

  if has("powerConsumptionReport") then
    local pc = val("powerConsumptionReport", "powerConsumption")
    if type(pc) == "table" then
      res.powerW = tonumber(pc.power)
      if tonumber(pc.energy) then res.energyKWh = tonumber(pc.energy) / 1000 end
    end
  end

  local mnmo = dig(main, "ocf", "mnmo", "value")
  if type(mnmo) == "string" then
    res.platform = mnmo:match("^([^|]+)")
    res.series = mnmo:match("^TP%dX_(%w+)")
  end
  local desc = dig(main, "samsungce.deviceIdentification", "description", "value")
  if type(desc) == "string" then res.series = desc:gsub("%c", ""):match("_([%u%d]+%w*)/") or res.series end
  res.firmware = dig(main, "ocf", "mnfv", "value")

  local f = res.features
  if res.control then f[#f + 1] = "Pause/Resume/Stop/Start" end
  if res.programme then f[#f + 1] = "Programme" end
  if res.spin then f[#f + 1] = "Spin" end
  if res.temperature then f[#f + 1] = "Temperature" end
  if res.rinses then f[#f + 1] = "Rinses" end
  if res.kidsLock ~= nil then f[#f + 1] = "Kids lock" end
  if res.remote ~= nil then f[#f + 1] = "Remote control" end
  if res.delayEnd ~= nil then f[#f + 1] = "Delay end" end
  if res.powerW then f[#f + 1] = "Power" end
  if res.errorReports then f[#f + 1] = "Error reports" end
  return res
end

------------------------------------------------------------------------------
-- State and cycle tracking
------------------------------------------------------------------------------
local function activePollSeconds()
  local s = tonumber(Properties["Poll Interval When On (seconds)"]) or 30
  return math.min(math.max(s, 20), 300)
end

-- A cycle whose last read showed this many minutes or fewer, and then ended, finished.
local function finishMargin()
  return math.max(3, math.ceil(activePollSeconds() / 60) + 2)
end

-- STATE from one status read, before what the driver remembers about the cycle.
local function rawState(res, online)
  if online == false then return "Off" end
  if #res.errors > 0 then return "Error" end
  if res.operating == "running" then return "Running" end
  if res.operating == "paused" then return "Paused" end
  if res.job == "finish" then return "Finished" end
  if res.switch == "off" then return "Off" end
  return "Ready"
end

local function minutesLeft(res, now)
  if res.remaining then return res.remaining end
  if res.completion and res.completion > now then return math.ceil((res.completion - now) / 60) end
  return nil
end

local function expectedFinish(res, now)
  if res.completion and res.completion > now then return res.completion end
  local m = minutesLeft(res, now)
  return m and (now + m * 60) or nil
end

-- When the cycle that just ended finished: the washer's completion time if it fits, else now.
local function finishedTime(res, now)
  local c = res.completion
  if c and gCycle.lastRunningAt and c >= gCycle.lastRunningAt - 120 and c <= now + 60 then return c end
  return now
end

local function applyPresentation(res, state, now)
  local inCycle = state == "Running" or state == "Paused"
  local left, finish, progress = nil, nil, 0
  if inCycle and gState.keptOffline and gCycle.expectedFinishAt then
    left = math.max(0, math.ceil((gCycle.expectedFinishAt - now) / 60))
    finish = gCycle.expectedFinishAt
    progress = res.progress or 0
  elseif inCycle then
    left = minutesLeft(res, now)
    finish = expectedFinish(res, now)
    progress = res.progress or 0
  elseif state == "Error" and gCycle.inCycle then
    left = minutesLeft(res, now)
    progress = res.progress or 0
  elseif state == "Finished" then
    left, progress = 0, 100
    local ts = ParseIsoUtc(gCycle.lastFinished)
    if ts and gCycle.finishedAt and math.abs(ts - gCycle.finishedAt) < 6 * 3600 then finish = ts end
  end
  local job = (inCycle or (state == "Error" and gCycle.inCycle)) and jobText(res.job) or ""

  setVar("STATE", state)
  setVar("JOB_STATE", job)
  setVar("PROGRAMME", res.programme or "")
  setVar("REMAINING_MINUTES", fmtNumber(left or 0))
  setVar("FINISH_TIME", finish and os.date("%H:%M", finish) or "")
  setVar("PROGRESS_PERCENT", fmtNumber(progress))
  setVar("SPIN", res.spin or "")
  setVar("TEMPERATURE", res.temperature or "")
  setVar("KIDS_LOCK", res.kidsLock and "1" or "0")
  setVar("REMOTE_CONTROL_ENABLED", res.remote and "1" or "0")
  setVar("LAST_FINISHED", gCycle.lastFinished or "")

  setProp("State", state .. (gState.keptOffline and " (offline, last known)" or ""))
  setProp("Job", job ~= "" and job or "-")
  setProp("Programme", res.programme or "Not available")
  setProp("Time Remaining", left and fmtMinutes(left) or "")
  setProp("Finish Time", finish and os.date("%H:%M", finish) or "")
  setProp("Progress", (inCycle or state == "Finished") and (fmtNumber(progress) .. " %") or "")
  setProp("Spin", res.spin or "Not available")
  setProp("Temperature", res.temperature or "Not available")
  setProp("Rinses", res.rinses or "Not available")
  setProp("Kids Lock", res.kidsLock == nil and "Not available" or (res.kidsLock and "On" or "Off"))
  setProp("Remote Control", res.remote == nil and "Not available"
    or (res.remote and "Enabled (Smart Control on)" or "Disabled"))
  setProp("Delay End", res.delayEnd == nil and "Not available"
    or (res.delayEnd > 0 and ("in " .. fmtMinutes(res.delayEnd)) or "Off"))
  setProp("Power", res.powerW and (fmtNumber(res.powerW) .. " W") or "Not available")
  setProp("Energy", res.energyKWh and (string.format("%.1f", res.energyKWh) .. " kWh total") or "Not available")
  local lf = ParseIsoUtc(gCycle.lastFinished)
  setProp("Last Finished", lf and fmtTime(lf) or "")
  local model = res.series or res.platform
  if model then
    local platform = (res.series and res.platform and not res.platform:find(res.series, 1, true)) and res.platform
    setProp("Model", model .. (platform and (" - " .. platform) or "")
      .. (res.firmware and (", firmware " .. res.firmware) or ""))
  end
  setProp("Reported Features", #res.features > 0 and table.concat(res.features, ", ") or "None")
  setProp("Last Update", os.date("%Y-%m-%d %H:%M:%S", now))
end

-- online: SmartThings health (true / false / nil when unknown)
function ApplyStatus(res, online)
  local now = os.time()
  local prev = gCycle.state
  local raw = rawState(res, online)
  local state = raw
  local events = {}
  gState.keptOffline = false

  if raw == "Running" or raw == "Paused" then
    if not gCycle.inCycle then
      gCycle.inCycle = true
      gCycle.stopAt = nil
      if prev ~= nil then events[#events + 1] = "Cycle Started" end
    end
    if raw == "Paused" and prev ~= nil and prev ~= "Paused" and prev ~= "Error" then
      events[#events + 1] = "Cycle Paused"
    end
    gCycle.lastRemaining = minutesLeft(res, now)
    gCycle.lastRunningAt = now
    gCycle.expectedFinishAt = expectedFinish(res, now)
    gCycle.finishedAt, gCycle.offSinceFinish = nil, false
  elseif raw ~= "Error" and gCycle.inCycle then
    -- The cycle is no longer running: finished, cancelled, or the washer dropped offline.
    local finished
    if raw == "Finished" then
      finished = true
    elseif gCycle.stopAt and now - gCycle.stopAt <= STOP_GRACE_S then
      finished = false                                  -- stopped with the Stop command
    elseif gCycle.lastRemaining and gCycle.lastRemaining <= finishMargin() and prev ~= "Error" then
      finished = true                                   -- finished between two reads
    elseif online == false and (prev == "Running" or prev == "Paused")
        and gCycle.expectedFinishAt and now <= gCycle.expectedFinishAt + OFFLINE_GRACE_S then
      finished = nil                                    -- offline mid-cycle: keep the last state
      state = prev
      gState.keptOffline = true
    else
      finished = false
    end
    if finished ~= nil then
      gCycle.inCycle, gCycle.stopAt = false, nil
      if finished then
        state = "Finished"
        gCycle.finishedAt = now
        gCycle.offSinceFinish = (raw == "Off")
        gCycle.lastFinished = isoUtc(finishedTime(res, now))
        events[#events + 1] = "Cycle Finished"
        log("Cycle finished at " .. gCycle.lastFinished)
      else
        log("Cycle ended without finishing (stopped, cancelled or offline) - not counted as finished")
      end
    end
  elseif raw == "Finished" then
    if not gCycle.finishedAt then gCycle.finishedAt = now end
  elseif gCycle.finishedAt and (raw == "Ready" or raw == "Off") then
    -- Keep showing Finished until the washer is switched on again, or for Show Finished For.
    if raw == "Off" then gCycle.offSinceFinish = true end
    local keepMin = tonumber(Properties["Show Finished For (minutes)"]) or 120
    local expired = keepMin > 0 and now - gCycle.finishedAt >= keepMin * 60
    if expired or (raw == "Ready" and gCycle.offSinceFinish) then
      gCycle.finishedAt, gCycle.offSinceFinish = nil, false
    else
      state = "Finished"
    end
  end
  if raw == "Error" then
    if prev ~= nil and prev ~= "Error" then events[#events + 1] = "Error" end
    if prev ~= "Error" then log("Washer reports an error: " .. table.concat(res.errors, ", ")) end
  end

  if res.remote ~= nil then
    if gCycle.remote ~= nil and res.remote ~= gCycle.remote then
      events[#events + 1] = res.remote and "Remote Control Enabled" or "Remote Control Disabled"
    end
    gCycle.remote = res.remote
  end

  if state ~= prev then log("State: " .. tostring(prev or "-") .. " -> " .. state) end
  gCycle.state = state
  gState.res = res
  gState.lastRead = now
  applyPresentation(res, state, now)
  persistCycle()
  for _, e in ipairs(events) do fireEvent(e) end
end

------------------------------------------------------------------------------
-- Polling: often while the washer is on, rarely while it is off
------------------------------------------------------------------------------
local function pollIntervalMs()
  local st = gCycle.state
  local on = next(gOps) ~= nil or st == "Running" or st == "Paused" or st == "Ready" or st == "Error"
    or (st == "Finished" and not gCycle.offSinceFinish and gState.online ~= false)
  if on then return activePollSeconds() * 1000 end
  local m = tonumber(Properties["Poll Interval When Off (minutes)"]) or 2
  return math.min(math.max(m, 1), 60) * 60 * 1000
end

local function pollTick()
  if deviceId() == "" or not bearerToken() or next(gOps) ~= nil then return end
  if gState.rateLimitedUntil and os.time() < gState.rateLimitedUntil then return end
  RefreshStatus()
end

local function updatePolling(force)
  local ms = pollIntervalMs()
  if force or ms ~= gPollMs then
    gPollMs = ms
    setTimer("poll", ms, pollTick, true)
  end
end

local function refreshSummary()
  if gState.holdUntil and os.time() < gState.holdUntil then return end
  if gState.authProblem then
    setDriverStatus("Re-authorization required")
  elseif gState.online == false then
    setDriverStatus("Washer offline in SmartThings (switched off or not on Wi-Fi)")
  elseif gCycle.state == "Error" and gState.res then
    setDriverStatus("Washer reports an error: " .. table.concat(gState.res.errors, ", "))
  elseif gCycle.state then
    setDriverStatus("OK")
  end
end

-- cb(ok, err) optional. skipHealth: status only (command confirmation reads, to stay well
-- inside SmartThings' per-device rate limit).
function RefreshStatus(cb, skipHealth)
  if deviceId() == "" then
    setDriverStatus("No washer selected - run Discover Washers")
    if cb then cb(false, "no washer selected") end
    return
  end
  apiRequest("GET", devicePath("/status"), nil, function(ok, code, data, err)
    if not (ok and type(data) == "table") then
      err = err or "empty answer"
      if code == 429 then gState.rateLimitedUntil = os.time() + RATE_LIMIT_PAUSE_S end
      gState.readFailures = gState.readFailures + 1
      gState.lastReadError = err
      logError("Status refresh failed: " .. tostring(err))
      if not (gState.holdUntil and os.time() < gState.holdUntil) then
        setDriverStatus("Status refresh failed: " .. tostring(err))
      end
      gState.refreshError = "status refresh failed"
      UpdateTile()
      if cb then cb(false, err) end
      return
    end
    local parsedOk, res = pcall(ParseDeviceStatus, data)
    if not parsedOk then
      logError("Could not parse status: " .. tostring(res))
      if cb then cb(false, "could not read the washer's status") end
      return
    end
    gState.lastStatus = data
    local function finish(online)
      if online ~= nil then
        if gState.online ~= nil and gState.online ~= online then
          log("Washer is now " .. (online and "online" or "offline") .. " in SmartThings")
        end
        gState.online = online
        setProp("Connection", online and "Online" or "Offline")
      end
      local applied, aerr = pcall(ApplyStatus, res, gState.online)
      if not applied then logError("Could not apply status: " .. tostring(aerr)) end
      gState.refreshError = nil
      refreshSummary()
      UpdateTile()
      updatePolling()
      if cb then cb(true) end
    end
    if skipHealth then
      finish(nil)
      return
    end
    apiRequest("GET", devicePath("/health"), nil, function(hok, _, hdata)
      local s = hok and dig(hdata, "state")
      local online
      if s == "ONLINE" then online = true elseif s == "OFFLINE" then online = false end
      finish(online)
    end)
  end)
end

------------------------------------------------------------------------------
-- Command confirmation: after SmartThings accepts a command, re-read the status
-- until the washer reports the new state (HTTP 200 alone does not mean it changed).
------------------------------------------------------------------------------
local runConfirm

local function scheduleConfirm()
  local nextDue
  for _, op in pairs(gOps) do
    if not nextDue or op.due < nextDue then nextDue = op.due end
  end
  if not nextDue then
    cancelTimer("confirm")
    return
  end
  local delay = math.max(1, nextDue - os.time())
  setTimer("confirm", delay * 1000, runConfirm)
end

function runConfirm()
  RefreshStatus(function()
    local now = os.time()
    for key, op in pairs(gOps) do
      if op.check() then
        gOps[key] = nil
        log(op.label .. " confirmed by the washer")
        op.settle(true)
      elseif now >= op.due then
        op.attempt = op.attempt + 1
        if op.attempt > #CONFIRM_DELAYS_S then
          gOps[key] = nil
          op.settle(false)
          commandFailed(op.label .. " was not confirmed by the washer")
        else
          op.due = now + CONFIRM_DELAYS_S[op.attempt]
        end
      end
    end
    UpdateTile()
    scheduleConfirm()
    updatePolling()
  end, true)
end

local function startConfirm(key, label, check, settle)
  gOps[key] = { label = label, check = check, settle = settle, attempt = 1, due = os.time() + CONFIRM_DELAYS_S[1] }
  scheduleConfirm()
end

------------------------------------------------------------------------------
-- Machine commands: washerOperatingState.setMachineState
------------------------------------------------------------------------------
MACHINE = {
  START  = { label = "Start", verb = "starting", target = "run", drum = true, from = { ready = true, finished = true },
             done = function() return gCycle.state == "Running" end },
  RESUME = { label = "Resume", verb = "resuming", target = "run", drum = true, from = { paused = true },
             done = function() return gCycle.state == "Running" end },
  PAUSE  = { label = "Pause", verb = "pausing", target = "pause", from = { running = true },
             done = function() return gCycle.state == "Paused" end },
  STOP   = { label = "Stop", verb = "stopping", target = "stop", from = { running = true, paused = true },
             done = function()
               return not gState.keptOffline and gCycle.state ~= "Running" and gCycle.state ~= "Paused"
             end },
}

local function preflight(label)
  if not bearerToken() then
    commandFailed(label .. " not sent - not authorized, complete the SmartThings setup")
    return false
  end
  if deviceId() == "" then
    commandFailed(label .. " not sent - no washer selected")
    return false
  end
  return true
end

-- Why a command must not be sent now, or nil. Uses a status read made just before.
local function refusal(name, m, res)
  if gState.online == false then return "the washer is offline in SmartThings" end
  if not res.control then return "this washer does not accept it through SmartThings (" .. res.controlReason .. ")" end
  if res.supportedMachineStates and not listHas(res.supportedMachineStates, m.target) then
    return "this washer does not accept '" .. m.target .. "'"
  end
  if m.drum and #res.errors > 0 then
    return "the washer reports an error (" .. table.concat(res.errors, ", ") .. ") - check the washer"
  end
  if m.drum and res.switch == "off" then return "the washer is switched off" end
  local op = res.operating
  if op == "ready" and res.job == "finish" then op = "finished" end
  if not m.from[op or ""] then
    if name == "START" then
      if op == "running" then return "the washer is already running" end
      if op == "paused" then return "the cycle is paused - use Resume" end
    end
    if name == "RESUME" then return "the washer is not paused" end
    if name == "PAUSE" then return "the washer is not running" end
    if name == "STOP" then return "no cycle is running" end
    return "the washer is not ready"
  end
  if m.drum and res.remote ~= true then return SMART_CONTROL end
  return nil
end

function MachineCommand(name)
  local m = MACHINE[name]
  if not m or not preflight(m.label) then return end
  log(m.label .. " requested - reading the washer first")
  -- Always decide on a fresh read: remote control can be switched off at the washer at any time.
  RefreshStatus(function(ok, err)
    if not ok then
      commandFailed(m.label .. " not sent - cannot read the washer: " .. tostring(err))
      return
    end
    local why = refusal(name, m, gState.res)
    if why then
      commandFailed(m.label .. " refused: " .. why)
      return
    end
    gSeq = gSeq + 1
    local seq = gSeq
    gState.pending = { label = m.label, verb = m.verb, seq = seq }
    UpdateTile()
    sendCommand("main", "washerOperatingState", "setMachineState", { m.target }, function(sent, code, serr)
      if not (gState.pending and gState.pending.seq == seq) then return end   -- superseded
      if not sent then
        gState.pending = nil
        if code == 409 or code == 422 then
          commandFailed(m.label .. ": the washer refused it (" .. tostring(serr) .. ")")
        else
          commandFailed(m.label .. ": " .. tostring(serr))
        end
        return
      end
      if name == "STOP" then gCycle.stopAt = os.time() end
      setDriverStatus(m.label .. " sent - waiting for the washer")
      startConfirm("machine", m.label, m.done, function(confirmed)
        if gState.pending and gState.pending.seq == seq then gState.pending = nil end
        if confirmed then setDriverStatus(m.label .. " done", STATUS_HOLD_S) end
        UpdateTile()
      end)
      updatePolling()
    end)
  end)
end

------------------------------------------------------------------------------
-- Discovery
------------------------------------------------------------------------------
local function cleanLabel(s)
  return trim((tostring(s or "Washer"):gsub(",", " ")))
end

function IsWasher(d)
  local ocfType = dig(d, "ocf", "ocfDeviceType")
  if ocfType == "oic.d.washer" then return true end
  if type(d.presentationId) == "string" and d.presentationId:find("^DA%-WM%-WM%-") then return true end
  if type(d.deviceTypeName) == "string" and (" " .. d.deviceTypeName .. " "):find("[^%a]Washer[^%a]") then
    return true
  end
  for _, comp in ipairs(d.components or {}) do
    if comp.id == "main" then
      for _, cat in ipairs(comp.categories or {}) do
        if cat.name == "Washer" then return true end
      end
    end
  end
  return false
end

local function publishDeviceList(items)
  gDevices = {}
  local names = {}
  local currentId = deviceId()
  local selected
  for _, d in ipairs(items) do
    local label = cleanLabel(d.label or d.name) .. " (" .. tostring(d.deviceId):sub(1, 8) .. ")"
    gDevices[label] = d.deviceId
    names[#names + 1] = label
    if d.deviceId == currentId then selected = label end
  end
  if #names == 0 then
    C4:UpdatePropertyList("Select Washer", DISCOVER_PROMPT, DISCOVER_PROMPT)
    return
  end
  if not selected and currentId == "" and #names == 1 then
    selected = names[1]
    setProp("Device ID", items[1].deviceId)
    log("Auto-selected the only washer found: " .. selected)
  end
  if not selected then
    table.insert(names, 1, DISCOVER_PROMPT)
    selected = DISCOVER_PROMPT
  end
  C4:UpdatePropertyList("Select Washer", table.concat(names, ","), selected)
end

local function fetchAllDevices(url, acc, cb)
  apiRequest("GET", url, nil, function(ok, code, data, err)
    if not ok then
      cb(false, err)
      return
    end
    for _, d in ipairs(dig(data, "items") or {}) do acc[#acc + 1] = d end
    local nextUrl = dig(data, "_links", "next", "href")
    if type(nextUrl) == "string" and nextUrl ~= "" and #acc < 2000 then
      fetchAllDevices(nextUrl, acc, cb)
    else
      cb(true, acc)
    end
  end)
end

function DiscoverDevices(silent)
  if not silent then setDriverStatus("Discovering washers...") end
  fetchAllDevices("/devices", {}, function(ok, all)
    if not ok then
      logError("Discovery failed: " .. tostring(all))
      setDriverStatus("Discovery failed: " .. tostring(all))
      return
    end
    local list = {}
    for _, d in ipairs(all) do
      if IsWasher(d) then list[#list + 1] = d end
    end
    log("Discovery found " .. #list .. " washer(s) among " .. #all .. " device(s)")
    publishDeviceList(list)
    if silent then return end
    if #list == 0 then
      setDriverStatus("No washers found in this SmartThings account")
    elseif deviceId() == "" then
      setDriverStatus("Select your washer in 'Select Washer'")
    else
      setDriverStatus("Washer selected - reading status")
      RefreshStatus()
    end
  end)
end

------------------------------------------------------------------------------
-- OAuth app creation (uses the Personal Access Token once) and authorization
------------------------------------------------------------------------------
function CreateOAuthApp()
  local pat = trim(Properties["Personal Access Token"])
  local clientId, _, redirect = oauthClient()
  if pat == "" then
    setProp("Authorization Status", "Paste a Personal Access Token first (see Documentation)")
    return
  end
  if clientId ~= "" then
    setProp("Authorization Status", "OAuth Client ID is already set - clear it first to create a new app")
    return
  end
  if redirect == "" then
    setProp("Authorization Status", "Set OAuth Redirect URI first")
    return
  end
  local body = {
    appName = "directorlink-samsung-washer-" .. randomHex(8) .. "-" .. randomHex(4) .. "-" .. randomHex(12),
    displayName = "DirectorLink Samsung Washer",
    description = "DirectorLink Control4 driver for Samsung washing machines",
    appType = "API_ONLY",
    classifications = { "CONNECTED_SERVICE" },
    singleInstance = true,
    principalType = "LOCATION",
    apiOnly = {},
    oauth = {
      clientName = "DirectorLink Samsung Washer",
      scope = OAUTH_SCOPES,
      redirectUris = { redirect },
    },
  }
  setProp("Authorization Status", "Creating OAuth app...")
  local headers = {
    ["Authorization"] = "Bearer " .. pat,
    ["Content-Type"] = "application/json",
    ["Accept"] = "application/json",
    ["User-Agent"] = USER_AGENT,
  }
  httpRequest("POST", API_BASE .. "/apps", JSON.encode(body), headers, function(errCode, errMsg, code, respBody)
    if errCode ~= 0 or not code then
      setProp("Authorization Status", "Create app failed: network error " .. tostring(errMsg))
      return
    end
    local decoded = safeDecode(respBody)
    if code >= 200 and code < 300 and dig(decoded, "oauthClientId") then
      C4:UpdateProperty("OAuth Client ID", decoded.oauthClientId)
      C4:UpdateProperty("OAuth Client Secret", decoded.oauthClientSecret or "")
      log("OAuth app created (appId " .. tostring(dig(decoded, "app", "appId")) .. ")")
      updateAuthUrl()
      setProp("Authorization Status",
        "OAuth app created - open the Authorization URL in a browser, then paste the code")
      ShowAuthUrl()
    else
      local msg = describeHttpError(code, decoded)
      if code == 401 or code == 403 then
        msg = msg .. " - the token needs ALL Apps scopes and must not be expired (see Documentation)"
      end
      logError("Create OAuth app failed: " .. msg)
      setProp("Authorization Status", "Create app failed: " .. msg)
    end
  end, true)
end

function ShowAuthUrl()
  local url = buildAuthUrl()
  updateAuthUrl()
  if url == "" then
    log("Set OAuth Client ID and Redirect URI first")
    return
  end
  print("")
  print("==== SmartThings authorization ====")
  print("1) Open this URL in a browser and sign in with the Samsung account that owns the washer:")
  print(url)
  print("2) Choose the location, approve, and copy the address of the page you land on (it contains code=...).")
  print("3) Paste it into the 'Authorization Code' property within a few minutes (codes expire).")
  print("===================================")
end

-- redirectOverride: the redirect URI to send when the property was just repaired
local function exchangeAuthCode(raw, redirectOverride)
  local code = extractAuthCode(raw)
  if not code or code == "" then
    setProp("Authorization Status", "Could not find a code in the pasted text")
    return
  end
  local clientId, _, redirect = oauthClient()
  redirect = redirectOverride or redirect
  if redirect == "" then
    setProp("Authorization Status", "Set OAuth Redirect URI first")
    return
  end
  setProp("Authorization Status", "Exchanging authorization code...")
  tokenRequest({
    { "grant_type", "authorization_code" },
    { "code", code },
    { "client_id", clientId },
    { "redirect_uri", redirect },
  }, function(ok, err)
    if ok then
      log("Authorization successful")
      setDriverStatus("Authorized - discovering washers")
      UpdateTile()
      DiscoverDevices()
    else
      logError("Authorization code exchange failed: " .. tostring(err))
      setProp("Authorization Status", "Authorization failed: " .. tostring(err) ..
        " (codes expire within minutes - open the URL again)")
    end
  end)
end

------------------------------------------------------------------------------
-- Print Diagnostics: everything a beta tester needs to send, never a token or secret,
-- and from the washer only the capabilities this driver reads (status answers also
-- hold serial numbers, which are not printed).
------------------------------------------------------------------------------
local DIAG_ATTRS = {
  { "switch", "switch" },
  { "samsungce.washerOperatingState", "operatingState" }, { "samsungce.washerOperatingState", "washerJobState" },
  { "samsungce.washerOperatingState", "washerJobPhase" }, { "samsungce.washerOperatingState", "remainingTime" },
  { "samsungce.washerOperatingState", "remainingTimeStr" }, { "samsungce.washerOperatingState", "progress" },
  { "samsungce.washerOperatingState", "operationTime" },
  { "washerOperatingState", "machineState" }, { "washerOperatingState", "washerJobState" },
  { "washerOperatingState", "completionTime" }, { "washerOperatingState", "supportedMachineStates" },
  { "samsungce.washerCycle", "washerCycle" }, { "samsungce.washerCycle", "referenceTable" },
  { "washerMode", "washerMode" },
  { "custom.washerSpinLevel", "washerSpinLevel" }, { "custom.washerWaterTemperature", "washerWaterTemperature" },
  { "custom.washerRinseCycles", "washerRinseCycles" }, { "samsungce.kidsLock", "lockState" },
  { "remoteControlStatus", "remoteControlEnabled" }, { "samsungce.washerDelayEnd", "remainingTime" },
  { "powerConsumptionReport", "powerConsumption" },
  { "samsungce.errorAndAlarmState", "events" }, { "custom.error", "error" },
  { "custom.disabledCapabilities", "disabledCapabilities" },
}

local function controllerVersion()
  local ok, v = pcall(function() return C4:GetVersionInfo() end)
  if ok and type(v) == "table" and v.version then return tostring(v.version) end
  return "?"
end

local function diagValue(v)
  if v == nil then return "null" end
  local ok, s = pcall(JSON.encode, v)
  s = ok and s or tostring(v)
  if #s > 200 then s = s:sub(1, 200) .. "..." end
  return s
end

function PrintDiagnostics()
  local build = ""
  pcall(function() build = tostring(C4:GetDriverConfigInfo("version") or "") end)
  local res = gState.res
  local lines = {
    "===== DirectorLink · Samsung Washer - diagnostics =====",
    "Driver        : " .. DRIVER_VERSION .. (build ~= "" and (" (" .. build .. ")") or "") .. " on Control4 OS "
      .. controllerVersion() .. ", device " .. tostring(C4:GetDeviceID()),
    "Driver Status : " .. tostring(Properties["Driver Status"]),
    "Authorization : " .. tostring(Properties["Authorization Status"]) .. " - OAuth Client ID "
      .. (trim(Properties["OAuth Client ID"]) ~= "" and "set" or "not set") .. ", Client Secret "
      .. (trim(Properties["OAuth Client Secret"]) ~= "" and "set" or "not set") .. ", Personal Access Token "
      .. (trim(Properties["Personal Access Token"]) ~= "" and "set" or "not set") .. ", refresh token "
      .. ((gAuth and gAuth.refresh_token) and "saved" or "none"),
    "Washer        : " .. (deviceId() ~= "" and (deviceId():sub(1, 8) .. "...") or "none selected")
      .. ", " .. tostring(Properties["Model"] ~= "" and Properties["Model"] or "model ?")
      .. ", " .. (gState.online == nil and "health unknown" or (gState.online and "online" or "OFFLINE")),
    "State         : " .. tostring(gCycle.state or "-") .. (gState.keptOffline and " (offline, last known)" or "")
      .. ", job " .. tostring(Properties["Job"]) .. ", programme " .. tostring(Properties["Programme"])
      .. ", " .. tostring(Properties["Time Remaining"]) .. " left, finish " .. tostring(Properties["Finish Time"])
      .. ", progress " .. tostring(Properties["Progress"]),
    "Settings      : spin " .. tostring(Properties["Spin"]) .. ", temperature " .. tostring(Properties["Temperature"])
      .. ", rinses " .. tostring(Properties["Rinses"]) .. ", kids lock " .. tostring(Properties["Kids Lock"])
      .. ", remote control " .. tostring(Properties["Remote Control"]) .. ", delay end " .. tostring(Properties["Delay End"]),
    "Cycle         : in cycle " .. tostring(gCycle.inCycle) .. ", last minutes left " .. tostring(gCycle.lastRemaining)
      .. ", Finished shown since " .. (gCycle.finishedAt and fmtTime(gCycle.finishedAt) or "-")
      .. ", LAST_FINISHED " .. (gCycle.lastFinished ~= "" and gCycle.lastFinished or "-"),
    "Control       : " .. ((res and res.control) and ("washerOperatingState.setMachineState ("
      .. (res.supportedMachineStates and table.concat(res.supportedMachineStates, ", ") or "run, pause, stop") .. ")")
      or ("not available" .. ((res and res.controlReason) and (" - " .. res.controlReason) or ""))),
    "Polling       : every " .. math.floor((gPollMs or 0) / 1000) .. " s now (" .. activePollSeconds()
      .. " s while on, " .. (tonumber(Properties["Poll Interval When Off (minutes)"]) or 2) .. " min while off)"
      .. ", last read " .. (gState.lastRead and os.date("%H:%M:%S", gState.lastRead) or "never")
      .. ", " .. gState.readFailures .. " failed reads" .. (gState.lastReadError and (" (last: " .. gState.lastReadError .. ")") or "")
      .. ((gState.rateLimitedUntil and os.time() < gState.rateLimitedUntil) and ", RATE LIMITED" or ""),
  }
  local st = gState.lastStatus
  local comps = dig(st, "components")
  if type(comps) == "table" then
    local main = comps.main or {}
    local names = {}
    for n in pairs(comps) do names[#names + 1] = n end
    table.sort(names)
    lines[#lines + 1] = "Components    : " .. table.concat(names, ", ")
    lines[#lines + 1] = "Reported by the washer (main component, the capabilities this driver reads):"
    local read, missing = {}, {}
    for _, a in ipairs(DIAG_ATTRS) do
      local cap, attr = a[1], a[2]
      read[cap] = true
      if type(main[cap]) == "table" then
        local entry = main[cap][attr]
        local ok, why = capabilityAvailable(comps, "main", cap)
        local line = "   " .. cap .. "." .. attr .. " = " .. diagValue(dig(entry, "value"))
          .. ((type(entry) == "table" and entry.unit) and (" " .. tostring(entry.unit)) or "")
          .. ((not ok and cap ~= "custom.disabledCapabilities") and (" [" .. why .. "]") or "")
        if cap == "samsungce.washerCycle" and attr == "washerCycle" and res and res.programme then
          line = line .. " -> " .. res.programme
        end
        lines[#lines + 1] = line
      elseif not listHas(missing, cap) then
        missing[#missing + 1] = cap
      end
    end
    lines[#lines + 1] = "   not reported: " .. (#missing > 0 and table.concat(missing, ", ") or "-")
    local others = {}
    for cap in pairs(main) do
      if not read[cap] then others[#others + 1] = cap end
    end
    table.sort(others)
    lines[#lines + 1] = "Other capabilities (names only): " .. (#others > 0 and table.concat(others, ", ") or "-")
  else
    lines[#lines + 1] = "Reported by the washer: no status read yet"
  end
  lines[#lines + 1] = "Last events   : " .. (#gEventLog > 0 and table.concat(gEventLog, "; ") or "none")
  lines[#lines + 1] = "===== end of diagnostics ====="
  for _, l in ipairs(lines) do print(l) end
  return lines
end

------------------------------------------------------------------------------
-- Housekeeping
------------------------------------------------------------------------------
local function startHousekeeping()
  setTimer("housekeeping", HOUSEKEEPING_MS, function()
    maybeRenewToken()
    updateAuthStatus()
  end, true)
end

local STATUS_PROPS = { "State", "Job", "Programme", "Time Remaining", "Finish Time", "Progress", "Spin",
  "Temperature", "Rinses", "Kids Lock", "Remote Control", "Delay End", "Power", "Energy", "Last Finished",
  "Model", "Reported Features" }

local function resetDeviceState()
  gOps = {}
  cancelTimer("confirm")
  gCycle = newCycle()
  gState.res, gState.lastStatus, gState.online, gState.pending = nil, nil, nil, nil
  gState.keptOffline, gState.refreshError, gState.lastRead = false, nil, nil
  gState.readFailures, gState.lastReadError, gState.rateLimitedUntil = 0, nil, nil
  for _, p in ipairs(STATUS_PROPS) do setProp(p, "") end
  setVar("STATE", "")
  setVar("JOB_STATE", "")
  setVar("PROGRAMME", "")
  setVar("REMAINING_MINUTES", "0")
  setVar("FINISH_TIME", "")
  setVar("PROGRESS_PERCENT", "0")
  setVar("SPIN", "")
  setVar("TEMPERATURE", "")
  setVar("KIDS_LOCK", "0")
  setVar("REMOTE_CONTROL_ENABLED", "0")
  setVar("LAST_FINISHED", "")
  persistCycle()
end

------------------------------------------------------------------------------
-- Control4 entry points
------------------------------------------------------------------------------
ON_PROPERTY_CHANGED = {}

ON_PROPERTY_CHANGED["Debug Mode"] = function(value)
  gDebug = (value == "On")
end

-- The page the browser lands on after authorizing, e.g. https://httpbin.org/get?code=Ab12Cd
local function isAuthResult(value)
  return trim(value):find("^https?://[^%s]*[?&]code=[^&#%s]+") ~= nil
end

ON_PROPERTY_CHANGED["Personal Access Token"] = function(value)
  if isAuthResult(value) then
    -- Authorization result pasted into the wrong field: use it, and clear the field
    log("Authorization result was pasted into Personal Access Token - using it as the authorization code")
    C4:UpdateProperty("Personal Access Token", "")
    exchangeAuthCode(value)
    return
  end
  updateAuthStatus()
  UpdateTile()
end

ON_PROPERTY_CHANGED["OAuth Client ID"] = function() updateAuthUrl() end
ON_PROPERTY_CHANGED["OAuth Client Secret"] = function() updateAuthUrl() end

ON_PROPERTY_CHANGED["OAuth Redirect URI"] = function(value)
  if isAuthResult(value) then
    -- Authorization result pasted into the wrong field: SmartThings appended ?code=...
    -- to the registered redirect URI, so restore that and use the code.
    local base = trim(value):match("^([^?#]+)")
    log("Authorization result was pasted into OAuth Redirect URI - restoring it to " .. base ..
      " and using the code")
    C4:UpdateProperty("OAuth Redirect URI", base)
    updateAuthUrl()
    exchangeAuthCode(value, base)
    return
  end
  updateAuthUrl()
end

ON_PROPERTY_CHANGED["Authorization Code"] = function(value)
  if trim(value) == "" then return end
  C4:UpdateProperty("Authorization Code", "")
  exchangeAuthCode(value)
end

ON_PROPERTY_CHANGED["Select Washer"] = function(value)
  local id = gDevices[value]
  if id and id ~= deviceId() then
    C4:UpdateProperty("Device ID", id)
    ON_PROPERTY_CHANGED["Device ID"](id)
  end
end

ON_PROPERTY_CHANGED["Device ID"] = function()
  resetDeviceState()
  setProp("Connection", "Unknown")
  UpdateTile()
  updatePolling()
  if deviceId() ~= "" and bearerToken() then RefreshStatus() end
end

ON_PROPERTY_CHANGED["Poll Interval When On (seconds)"] = function() updatePolling(true) end
ON_PROPERTY_CHANGED["Poll Interval When Off (minutes)"] = function() updatePolling(true) end

function OnPropertyChanged(strProperty)
  local value = Properties[strProperty]
  dbg("OnPropertyChanged:", strProperty)
  local fn = ON_PROPERTY_CHANGED[strProperty]
  if fn then
    local ok, err = pcall(fn, value)
    if not ok then logError("OnPropertyChanged(" .. strProperty .. "): " .. tostring(err)) end
  end
end

ACTIONS = {
  CreateOAuthApp = function() CreateOAuthApp() end,
  ShowAuthUrl = function() ShowAuthUrl() end,
  DiscoverDevices = function() DiscoverDevices() end,
  RefreshStatus = function() RefreshStatus() end,
  Pause = function() MachineCommand("PAUSE") end,
  Resume = function() MachineCommand("RESUME") end,
  Stop = function() MachineCommand("STOP") end,
  Start = function() MachineCommand("START") end,
  PrintDiagnostics = function() PrintDiagnostics() end,
  RenewToken = function() RenewToken() end,
  SignOut = function()
    clearTokens(nil)
    setDriverStatus("Signed out")
  end,
}

COMMANDS = {
  START = function() MachineCommand("START") end,
  PAUSE = function() MachineCommand("PAUSE") end,
  RESUME = function() MachineCommand("RESUME") end,
  STOP = function() MachineCommand("STOP") end,
  REFRESH = function() RefreshStatus() end,
}

function ExecuteCommand(strCommand, tParams)
  tParams = tParams or {}
  dbg("ExecuteCommand:", strCommand, tParams.ACTION)
  local fn
  if strCommand == "LUA_ACTION" then
    fn = ACTIONS[tParams.ACTION or ""]
  else
    fn = COMMANDS[strCommand]
  end
  if fn then
    local ok, err = pcall(fn, tParams)
    if not ok then logError("ExecuteCommand(" .. tostring(strCommand) .. "): " .. tostring(err)) end
  else
    dbg("Unhandled command:", strCommand)
  end
end

-- A tap on the tile reads the washer again and shows its state. It never sends a command.
local function refreshFromTile()
  if not bearerToken() or deviceId() == "" then
    UpdateTile()
    return
  end
  gState.refreshing = true
  UpdateTile()
  RefreshStatus(function()
    gState.refreshing = false
    UpdateTile()
  end)
end

function ReceivedFromProxy(idBinding, strCommand, tParams)
  dbg("ReceivedFromProxy:", idBinding, strCommand)
  local ok, err = pcall(function()
    if idBinding == TILE_BINDING and strCommand == "SELECT" then refreshFromTile() end
  end)
  if not ok then logError("ReceivedFromProxy: " .. tostring(err)) end
end

function TestCondition(strName, tParams)
  local logic = tParams and tParams.LOGIC or "EQUAL"
  local value = tParams and tParams.VALUE or ""
  local current
  if strName == "STATE" then
    current = gCycle.state or ""
  elseif strName == "REMOTE_CONTROL" then
    current = gCycle.remote and "Enabled" or "Disabled"
  end
  if current == nil then return false end
  if logic == "NOT_EQUAL" then return current ~= value end
  return current == value
end

function OnDriverInit(strDIT)
  -- Variables must be added here, always in the same order: Director numbers them 1001, 1002, ...
  C4:AddVariable("STATE", "", "STRING", true, false)                 -- 1001
  C4:AddVariable("JOB_STATE", "", "STRING", true, false)             -- 1002
  C4:AddVariable("PROGRAMME", "", "STRING", true, false)             -- 1003
  C4:AddVariable("REMAINING_MINUTES", "0", "NUMBER", true, false)    -- 1004
  C4:AddVariable("FINISH_TIME", "", "STRING", true, false)           -- 1005
  C4:AddVariable("PROGRESS_PERCENT", "0", "NUMBER", true, false)     -- 1006
  C4:AddVariable("SPIN", "", "STRING", true, false)                  -- 1007
  C4:AddVariable("TEMPERATURE", "", "STRING", true, false)           -- 1008
  C4:AddVariable("KIDS_LOCK", "0", "BOOL", true, false)              -- 1009
  C4:AddVariable("REMOTE_CONTROL_ENABLED", "0", "BOOL", true, false) -- 1010
  C4:AddVariable("LAST_FINISHED", "", "STRING", true, false)         -- 1011
  gPublished = { STATE = "", JOB_STATE = "", PROGRAMME = "", REMAINING_MINUTES = "0", FINISH_TIME = "",
    PROGRESS_PERCENT = "0", SPIN = "", TEMPERATURE = "", KIDS_LOCK = "0", REMOTE_CONTROL_ENABLED = "0",
    LAST_FINISHED = "" }
end

function OnDriverLateInit(strDIT)
  math.randomseed(os.time() + (tonumber(C4:GetDeviceID()) or 0))
  setProp("Driver Version", DRIVER_VERSION ~= "dev" and DRIVER_VERSION or ("dev build " .. tostring(C4:GetDriverConfigInfo("version") or "")))
  OnPropertyChanged("Debug Mode")

  local saved = persistGet(PERSIST_AUTH, true)
  if type(saved) == "table" and saved.access_token then gAuth = saved end
  -- The cycle as last seen, so a restart neither repeats nor misses an event.
  local cycle = persistGet(PERSIST_CYCLE, false)
  if type(cycle) == "table" and cycle.deviceId and cycle.deviceId == deviceId() then
    for k, v in pairs(cycle) do gCycle[k] = v end
    gPersistedCycle = JSON.encode(gCycle)
    if gCycle.state then
      setVar("STATE", gCycle.state)
      setProp("State", gCycle.state)
    end
    setVar("LAST_FINISHED", gCycle.lastFinished or "")
  end

  updateAuthUrl()
  updateAuthStatus()
  UpdateTile()
  updatePolling(true)
  startHousekeeping()

  if not bearerToken() then
    setDriverStatus("Not configured - see the Documentation tab")
    return
  end
  -- Give the network a moment after a controller restart.
  setTimer("startup", 5000, function()
    maybeRenewToken(function()
      if deviceId() ~= "" then
        RefreshStatus()
        DiscoverDevices(true)  -- repopulate the selection list
      else
        DiscoverDevices()
      end
    end)
  end)
end

function OnDriverDestroyed(strDIT)
  for name in pairs(gTimers) do cancelTimer(name) end
end

log("Driver loaded")
