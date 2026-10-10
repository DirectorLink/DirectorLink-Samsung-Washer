# DirectorLink · Samsung Washer for Control4

Free, open-source Control4 driver for Samsung Wi-Fi washing machines. Part of [DirectorLink Drivers](https://directorlink.io/drivers).

It works through the Samsung SmartThings cloud: no extra hardware and no subscription.

Free. No subscription, no license key, no account with us.

Made by [DirectorLink](https://directorlink.io), the open-source management layer for Control4 homes. Works on its own: DirectorLink is not required.

DirectorLink is an independent project, not affiliated with Control4 or Snap One. Not affiliated with or endorsed by Samsung.

**Download:** [releases](../../releases) · **Help and updates:** [directorlink.io/drivers/samsung-washer](https://directorlink.io/drivers/samsung-washer)

> **Beta.** 1.0.0-beta.1 passes its tests against recorded SmartThings answers from real Samsung washers, but
> it has not run on a real washer yet. The first model to test is the **WW90CGC04DAH** (Series 5 ecobubble, 9 kg).
> If you install it, please see [Testing the beta](#testing-the-beta).

| Feature | Control4 app | Programming |
|---|---|---|
| **Washer** tile | icon: off, ready, running, paused, finished, error; a tap reads the washer again | – |
| State, job, programme, time left, finish time, progress | tile text | variables, State conditional |
| Spin, temperature, rinses, kids lock, delay end, power | – | variables, Composer properties |
| **Cycle Started / Paused / Finished**, **Error** | – | events (Cycle Finished once per cycle) |
| **Smart Control** (remote control) on or off | – | events, variable, conditional |
| **Pause**, **Resume**, **Stop** | – | commands and Composer actions |
| **Start** | – | command, only while Smart Control is on at the washer |

The driver never changes the programme, temperature or spin.

## Requirements

- **Control4:** OS 3.3 or newer, with internet access on the controller.
- **Washer:** a Samsung washing machine on Wi-Fi, added to the SmartThings app.
- **For setup:** the login of the Samsung account the washer is registered to, and about 10 minutes.
- **Tested on:**
  - Live: none yet (the beta). First model to test: WW90CGC04DAH.
  - Offline: recorded SmartThings answers from a DA_WM_TP1_21_COMMON washer (the platform of the WW5000C series,
    which includes the WW90CGC04DAH), a TP2 front-loader, an A51 washer-dryer and two older TP6X washers, under Lua 5.1 and LuaJIT.

## Installation

1. Download **`DirectorLink-Samsung-Washer.c4z`** from the [releases](../../releases),
   or from https://directorlink.io/drivers/samsung-washer. Each release also lists `SHA256SUMS.txt`.
2. In Composer Pro: **Driver → Add or Update Driver or Agent**, then select the file.
3. On the Search tab, tick **Local**, search for **DirectorLink**, and add **DirectorLink · Samsung Washer** to the room.

> **Keep the file name exactly `DirectorLink-Samsung-Washer.c4z`.** Composer identifies a driver by its file name.
> A browser saves a second download as `DirectorLink-Samsung-Washer (1).c4z`, which Composer installs as a
> *separate* driver instead of updating this one. Delete older downloads first.

## Setup

The same steps appear on the driver's **Documentation** tab in Composer. They are the same as for DirectorLink ·
Samsung Refrigerator: if a project already has it, you still create a token and sign in once for the washer.

### Step 1: Get the right SmartThings token

The driver needs a **Personal Access Token** **once**, to create its own secure login (OAuth).

1. On a computer, open **https://account.smartthings.com/tokens**.
2. Sign in with the **same Samsung account the washer is registered to** in the SmartThings app.
3. Click **Generate new token** and give it any name, for example `Control4`.
4. Under **Authorized Scopes**, **tick every checkbox in these three sections:**
   - **Devices**: all boxes
   - **Locations**: all boxes
   - **Apps**: all boxes

   If you're unsure, tick every box on the page. The token is used once and expires by itself after 24 hours.
5. Click **Generate token** and copy it. It looks like `1a2b3c4d-1111-2222-3333-444455556666`
   and is shown **only once**.
6. In Composer, select the driver and paste the token into **Personal Access Token** → **Set**.

### Step 2: Create the OAuth app (this is where the Client ID comes from)

On the driver's **Actions** tab click **1. Create OAuth App (uses Personal Access Token)**.
**OAuth Client ID** and **OAuth Client Secret** fill in automatically.

> You don't need to look up a Client ID anywhere: the driver creates it. SmartThings never shows the
> secret again, so **save both values**. If you reinstall the driver, paste the saved Client ID and
> Secret instead of creating another app.

### Step 3: Sign in once

1. Click **2. Show Authorization URL**. The link appears on the **Lua** tab and in the **Authorization URL** property.
2. Open it in a browser, sign in with the same Samsung account, choose the location, and click **Authorize**.
3. The browser lands on `https://httpbin.org/get?code=...`. Copy the **whole address** from the address bar.
4. **Within a few minutes**, paste it into the **Authorization Code** property → **Set**.
   Pasted it into **OAuth Redirect URI** or **Personal Access Token** by mistake? The driver notices,
   puts that field back, and signs in anyway.

**Authorization Status** should now say *Authorized (OAuth) … renews automatically*. You're done: the
driver renews its own access from now on, and the Personal Access Token is no longer used.

### Step 4: Select the washer and show the tile

- With one washer, it's selected automatically. Otherwise, choose it in **Select Washer**. Only washers are
  listed: dryers, dishwashers and other devices in the account are left out.
- **Reported Features** lists what your washer tells SmartThings.
- The driver appears in the room as **Washer**. In the room's Navigator settings, make the **Washer** tile visible.

## The Washer tile

The icon shows the washer's state: **gray** off, **soft blue** ready (switched on), **blue with water** running,
**amber with a pause badge** paused, **green with a check** finished, **red with "!"** error. The text under it
says more, for example *Washing - Eco 40-60, 1 h 40 min left, done 14:32* or *Finished at 14:32*.

**A tap on the tile only reads the washer again and shows its state.** It never pauses, starts or stops the
washer, so a child or a pocket can't stop a wash. Pausing and resuming from a touchscreen is left to
programming you add on purpose (for example a custom button running *Pause*).

## Programming

### Safe control

- **Pause**, **Resume** and **Stop** use SmartThings' `washerOperatingState.setMachineState`, as Composer actions
  and as programming commands. **Stop cancels the cycle.**
- **Start** and **Resume** set the drum moving, so they are sent only when the washer itself says remote control
  is enabled: someone pressed **Smart Control** on the washer. The driver reads the washer just before deciding.
  Otherwise the command is refused, **Driver Status** says *Start refused: Press Smart Control on the washer
  first*, **Command Failed** fires, and the driver **never retries by itself**.
- Every command reads the washer first and is refused with a clear reason when it can't work, for example
  *Pause refused: the washer is not running*. After a command, the driver re-reads the washer 3, 10, 25 and 55
  seconds later until the washer confirms. If it doesn't, the driver fires **Command Failed** and doesn't send it again.
- The driver never changes the programme, temperature, spin or any other setting.

### Events

| Event | When |
|---|---|
| **Cycle Started** | A cycle starts (at the washer, in the SmartThings app or from Control4). Resuming isn't a new cycle. |
| **Cycle Paused** | A running cycle is paused. |
| **Cycle Finished** | A cycle ends normally. Once per cycle: never repeated while the washer stays finished, and not repeated after a controller restart. A cycle that is stopped or cancelled before its end doesn't count. |
| **Error** | The washer reports an error (see [Testing the beta](#testing-the-beta)). |
| **Remote Control Enabled** / **Disabled** | Smart Control is turned on at the washer / turns off. |
| **Command Failed** | A command was refused, rejected or not confirmed by the washer. |
| **Authorization Required** | SmartThings no longer accepts the saved login. |

**Conditionals:** *State is* Off / Ready / Running / Paused / Finished / Error; *Remote Control is* Enabled / Disabled.

### Variables

Use them in programming conditions, or read them from other drivers. The ids never change.

| Id | Variable | Type | Meaning |
|---|---|---|---|
| 1001 | `STATE` | string | `Off`, `Ready`, `Running`, `Paused`, `Finished` or `Error`. Empty before the first status read. |
| 1002 | `JOB_STATE` | string | What a running or paused cycle is doing: `Washing`, `Rinsing`, `Spinning`, `Pre-wash`, `Drying`, `Weighing`, `Delay Start`, … Empty otherwise. |
| 1003 | `PROGRAMME` | string | The selected programme, for example `Eco 40-60` or `Cotton`. An unknown code shows as `Course 1C`. |
| 1004 | `REMAINING_MINUTES` | number | Minutes left in the cycle. `0` when no cycle is running. |
| 1005 | `FINISH_TIME` | string | When the cycle should end, local time `HH:MM`. When finished, when it ended. Empty otherwise. |
| 1006 | `PROGRESS_PERCENT` | number | Progress of the cycle, `0`–`100`. |
| 1007 | `SPIN` | string | Spin speed, for example `1400 rpm`, `Rinse Hold`, `No Spin`. |
| 1008 | `TEMPERATURE` | string | Water temperature, for example `40 °C`, `Cold`, `Warm`. |
| 1009 | `KIDS_LOCK` | bool | The child lock is on. |
| 1010 | `REMOTE_CONTROL_ENABLED` | bool | Smart Control is on: Start and Resume are allowed. |
| 1011 | `LAST_FINISHED` | string | When the last cycle finished, ISO 8601 UTC, for example `2026-10-10T14:32:05Z`. Kept across restarts. |

**When is it Finished?** From the end of a cycle until someone switches the washer on again, or until
**Show Finished For** (default 120 minutes; 0 = until it's switched on again) has passed. If the washer drops off
Wi-Fi in the middle of a cycle, the driver keeps showing the cycle (marked *offline, last known*) instead of
announcing a new one when it comes back.

### Examples

- **Laundry's done:** on **Cycle Finished**, send a push notification, or flash the hall lights.
- **Forgotten laundry:** on **Cycle Finished**, start a 30-minute timer; if *State is Finished* when it ends, notify again.
- **Quiet hours:** a scheduled event at 22:00 runs *Pause* if *State is Running*.

## Polling

SmartThings is read every **30 seconds** while the washer is on, running, paused or just finished
(**Poll Interval When On**), and every **2 minutes** while it is off or offline (**Poll Interval When Off**). Each
read is two requests, so the driver stays well inside SmartThings' limit of about 12 requests a minute per device.
If SmartThings answers *rate limited*, the driver pauses for two minutes. A cycle started at the washer
while it was off shows up within the off interval.

## Testing the beta

This beta hasn't run on a real washer yet. Your Print Diagnostics output is what turns it into 1.0.0. If you have a
Samsung washer in SmartThings (the WW90CGC04DAH or any other model), please:

1. Install the driver and complete the setup. Set **Debug Mode** to **On**.
2. With the washer switched on, run **Print Diagnostics** on the driver's Actions tab. Copy the whole output from
   the **Lua** tab, from `===== DirectorLink · Samsung Washer - diagnostics =====` to `===== end of diagnostics =====`.
3. Start a cycle at the washer and let it run to the end. While it runs, run **Print Diagnostics** once more, and
   check that the tile, **State**, **Programme**, **Time Remaining** and **Finish Time** match the washer's display.
4. If you can, press **Smart Control** on the washer and try **Pause** and **Resume** from the Actions tab.
   Try **Start** once with Smart Control off: it must be refused.
5. If the washer ever shows an error code on its display while you test, run **Print Diagnostics** while the code
   shows. No recorded washer has reported an error to SmartThings yet, so this is the part we know least about.
   Please don't cause an error on purpose.
6. Send all Print Diagnostics outputs, the washer's model number (on the label inside the door) and your Control4 OS
   version in an [issue](../../issues), and say what didn't match.

Print Diagnostics shows the driver version, the authorization state (never a token or secret), the washer's model
code, its state, and the value of every SmartThings capability this driver reads, plus the names (not the values)
of the others. It never prints the washer's serial number or the full device id. Remove anything you don't want to
share. Reports that everything works are just as welcome.

## Updating

Download the new `DirectorLink-Samsung-Washer.c4z` from the [releases](../../releases).
Mind the file name, as described in Installation. Then run **Driver → Add or Update Driver or Agent** in
Composer Pro. Your settings and sign-in are kept. To go back to an older version, install the `.c4z` from an
older release the same way.

## Troubleshooting

| What you see | What to do |
|---|---|
| `Start refused: Press Smart Control on the washer first` | Press **Smart Control** on the washer, then send Start again. The driver never starts the washer without it. |
| `... refused: the washer is switched off` | Switch the washer on first. |
| `... refused: this washer does not accept it through SmartThings` | Your model doesn't offer that control to SmartThings (some washer-dryers). The state is still shown. |
| `... was not confirmed by the washer` | SmartThings accepted the command but the washer didn't change. Check that it's online in the SmartThings app and that Smart Control is on. |
| `Washer offline in SmartThings` | Some washers leave Wi-Fi when switched off; then this is normal and **State** shows *Off*. Otherwise check the washer's Wi-Fi in the SmartThings app. |
| `Create app failed: Forbidden ...` | The token is missing scopes. Make a new one with **all Devices, Locations and Apps** boxes ticked. |
| `Create app failed: Not authorized ...` | The token is older than 24 hours. Make a new one. |
| `No washers found in this SmartThings account` | You signed in with a different Samsung account than the one the washer is registered to. |
| `Authorization failed: ...` | The code expired or was already used. Click **2. Show Authorization URL** and sign in again. |
| `Re-authorization required` | SmartThings no longer accepts the saved login, for example after about 30 days with the controller offline. Repeat Step 3 only. |
| `Rate limited by SmartThings` | Don't set **Poll Interval When On** very short. The driver pauses polling for two minutes by itself. |
| The programme shows as `Course 1C` | The driver doesn't know that course code's name yet. Send Print Diagnostics and the name on the washer's dial in an issue. |

## Privacy

The driver talks only to the Samsung cloud. It sends nothing to DirectorLink and collects no usage data.

The sign-in in Step 3 happens in your browser, which lands on httpbin.org to show you the one-time code.
SmartThings tokens are kept in Control4's encrypted driver storage and are never written to the log or shown
anywhere, including Print Diagnostics.

## Support

Community-supported, best effort. Report problems in [Issues](../../issues).

Include the **Print Diagnostics** output (it has the driver version, model and state), the washer's model number,
your Control4 OS version, and the Lua output with Debug Mode on. Never post tokens or the Client Secret.
For security problems, see [SECURITY.md](SECURITY.md).

## Building from source

```
src/driver.xml               driver definition (the Washer uibutton proxy, properties, actions, commands, events)
src/driver.lua               driver logic (OAuth, SmartThings API, state and cycle tracking, safe control, tile)
src/www/documentation.html   Composer Documentation tab
src/www/icons/washer/        tile icons per state + Composer images, generated by scripts/make_icons.py
scripts/gen_ui_xml.py        regenerates driver.xml's proxy, Navigator icons and connection
scripts/build.py             stamp VERSION, validate and package -> dist/DirectorLink-Samsung-Washer.c4z
scripts/check_repo.py        fail if secrets or local configuration would be committed
tests/test_driver.py         offline tests: real driver.lua vs mocked Control4 + fake SmartThings cloud
tests/live_test.py           live tests against a real washer (status and watch are read-only)
tests/make_fixtures.py       prepares tests/fixtures (see NOTICE)
tests/fixtures/              recorded SmartThings washer answers, and paused/finished/error/remote-off variants
```

```
pip install -r requirements-dev.txt
python tests/test_driver.py          # offline tests under Lua 5.1 and LuaJIT 2.1
python scripts/check_repo.py
python scripts/build.py              # -> dist/DirectorLink-Samsung-Washer.c4z

set ST_TOKEN=<personal access token> # read your washer through the real driver (read-only)
python tests/live_test.py status
python tests/live_test.py watch      # prints every state change and event during a cycle
```

**Releasing:** `VERSION` (`MAJOR.MINOR.PATCH`, or `MAJOR.MINOR.PATCH-beta.N`) is the only place the version is set.
The build stamps it into the package: `driver.xml` `<version>` = MAJOR·1000000 + MINOR·10000 + PATCH·100 + BUILD,
where BUILD is the beta number, or 99 for the release (`1.0.0-beta.1` → `1000001`, `1.0.0` → `1000099`), so each
beta and the release after it count up; and `DRIVER_VERSION` in `driver.lua`. Add `docs/releases/v<VERSION>.md` and
push to `main`. The release workflow runs the tests, builds, and publishes the release with the `.c4z` and
`SHA256SUMS.txt`, as a pre-release for `-beta` versions. Without the notes file nothing is published, and an existing
release is never replaced.

Don't zip with PowerShell 5.1's `Compress-Archive`: it writes backslash paths that break the icons.
Never change the `.c4z` file name, the proxy binding id, variable order, event and command ids, or property names
once released, because installed projects and dealers' programming depend on them. Director numbers variables
in the order `OnDriverInit` adds them (1001, 1002, …), so new variables are only ever added after the last one.

### SmartThings API used

| What | Capability (component `main`) |
|---|---|
| State | `samsungce.washerOperatingState` (`operatingState`, `washerJobState`, `remainingTime`, `progress`), else `washerOperatingState` (`machineState`, `washerJobState`, `completionTime`) |
| Programme | `samsungce.washerCycle` (`Table_02_Course_1C` → *Eco 40-60*), else `washerMode` |
| Settings | `custom.washerSpinLevel`, `custom.washerWaterTemperature`, `custom.washerRinseCycles`, `samsungce.kidsLock`, `samsungce.washerDelayEnd`, `remoteControlStatus`, `switch` |
| Errors | `samsungce.errorAndAlarmState` (`events`), `custom.error` |
| Power | `powerConsumptionReport` |
| Online | `GET /devices/{id}/health` |
| Pause / Resume / Stop / Start | `washerOperatingState` → `setMachineState("pause" / "run" / "stop")`, the command Home Assistant's `smartthings` integration uses |

A capability counts as missing when it isn't there or is listed in `custom.disabledCapabilities` (some
washer-dryers disable `washerOperatingState`). Auth: an OAuth2 API_ONLY app with scopes `r:devices:* x:devices:*`.
Access tokens last 24 hours. Refresh tokens rotate on every use, so each new one is saved immediately and
refreshes run one at a time.

The programme names come from course tables that the [localthings](https://github.com/mbillow/localthings) project
cross-checked on real washers. The recorded SmartThings answers in `tests/fixtures` are
[Home Assistant](https://github.com/home-assistant/core)'s test fixtures. See [NOTICE](NOTICE). Control4 interfaces
follow the public [Snap One DriverWorks documentation](https://snap-one.github.io/docs-driverworks-fundamentals/).
DirectorLink's own source code: [github.directorlink.io](https://github.directorlink.io).

## License and trademarks

See [LICENSE](LICENSE) and [NOTICE](NOTICE).

Apache License 2.0. Copyright 2026 DirectorLink. DirectorLink is an independent project, not affiliated with Control4 or Snap One. Not affiliated with or endorsed by Samsung. Samsung, SmartThings, Control4 and related names are trademarks of their respective owners.
