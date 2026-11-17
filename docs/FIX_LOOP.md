# Fix loop: root-cause, patch, build, flash, re-run until green

`xts-agent fix-loop` takes failing xTS tests and works on them the way an OEM
platform engineer would: run them on the DUT, find the root cause in the OEM
source (CarSettings, CarSystemUI, CarLauncher, device overlays, ...), write the
smallest source change, build the image, flash the DUT, run the tests again,
and keep the change only if the tests now pass twice in a row. A change that
does not build or does not help is reverted and the next attempt is told why.

```
baseline run ──► root cause ──► patch OEM source ──► build ──► flash DUT ──► re-run
     ▲          (device probes      (LLM; editable                             │
     │           + local LLM         OEM trees only)                           ▼
     │           + OEM source)                    still red: revert, retry ◄── green? ──► confirm ──► patch for review
```

Lab and host problems (a broken host tool, a DUT without network) are handled
by the same loop with allow-listed device-side remediations (below), so they do
not get in the way of the product fix, but they belong in lab setup.

## Code fixes

Configured under `fix_loop.code_fix`, with the OEM trees the loop may change
marked `editable: true` in `fix_loop.oem_sources`
(`config/test_plans/oem_code_fix.yaml` is a complete example):

```yaml
fix_loop:
  oem_sources:
    - {name: CarSettings, kind: local, path: /src/oem/packages/apps/Car/Settings,
       packages: [com.android.car.settings], editable: true, build_targets: [CarSettings]}
    - {name: DeviceOverlay, kind: local, path: /src/oem/device/oem/car,
       editable: true, build_targets: [framework-res]}
    - {name: AospFrameworkRes, kind: local, path: /src/oem/frameworks/base/core/res/res/values}  # read-only
  code_fix:
    enabled: true
    build_command: "scripts/aosp_build.sh {modules}"     # m <touched modules> && m
    deploy_command: "scripts/cf_flash.sh {serial}"       # fastboot flashall -w on hardware
    max_attempts: 2
    restore_on_failure: true
```

When is it used? When the diagnosis is a product bug and no device-side
remediation applies, or right after a device-side workaround has *proven* a
product root cause (`WORKAROUND_VERIFIED`): the workaround is undone and the
real fix is written in source.

Guard rails:

* Only files under editable sources change; CTS, tests and test configs never
  do (enforced by path, whatever the LLM returns).
* Each edit replaces one exact block that occurs once in a file the LLM was
  shown; at most 4 edits and ~60 changed lines.
* A patch that fails to build is reverted and the compiler errors go back to
  the LLM; one that builds but leaves the test red is reverted too.
* Before building, the LLM reviews its own diff against the measured facts
  (device probes, resource and UI-text checks). The review is a cheap filter,
  not the judge: its objections go back to the proposer, and a diff proposed
  again after a rejection is built and tested, because CTS on the DUT decides.
* Measured checks are given to the LLM as facts: which window holds focus and
  its flags; framework config defaults, whether the product's overlays override
  them and whether the default package is installed; UI strings whose siblings
  carry the text a test expects.
* Original file contents are restored from memory, so uncommitted OEM work in
  the same tree is kept. Nothing is committed or pushed: the verified patch is
  written to `results/fixloop/patches/<session>.patch` for review.
* If no attempt worked and something was flashed, the original source is
  rebuilt and reflashed so the DUT matches the tree again.

The target ends `FIXED_IN_CODE` with the diff, the build and flash times and
the TradeFed result directory of every run on the patched build.

```
baseline run ──► diagnose ──► apply one allow-listed remediation ──► re-run
     ▲             (rules + device probes                              │
     │              + local LLM + OEM code)                            │
     │                                                                 ▼
     └──────── improved: keep, go again ◄──── no change: revert ◄── green? ──► confirm run ──► done
```

Every step is a real TradeFed invocation. The page it produces
(`results/fixloop/fixloop_<stamp>.html`) shows each run's pass/fail counts as
read from that run's `test_result.xml`, with the result directory alongside.

## Quick start

```bash
# One or more targets: "<Module> [Class#method | Class ...]"
xts-agent fix-loop --serial 0.0.0.0:6527 \
  --test "CtsOsTestCases android.os.cts.StrictModeTest#testNetwork"

# Several DUTs in parallel; targets are dealt round-robin
xts-agent fix-loop --serial 0.0.0.0:6523 --serial 0.0.0.0:6524 \
  --test "CtsTelecomTestCases android.telecom.cts.SelfManagedConnectionTest" \
  --test "CtsWifiTestCases android.net.wifi.cts.WifiNetworkSuggestionTest"

# Take the worst modules from an existing run, keep cycling the unfixed ones
xts-agent fix-loop --serial 0.0.0.0:6520 \
  --from-results /opt/xts/android-cts/results/<session> --top 5 --until-pass
```

If a DUT hangs or drops mid-run, that run's result is discarded, the DUT is
reconnected and then restarted with `fix_loop.restart_command` (this lab:
`restart_cvd -instance_num={cvd_instance}`); if it still does not come back,
the target moves to a `--spare` DUT and starts again from a new baseline there.

Options: `--spare <serial>` (repeatable), `--max-iterations` (TradeFed runs per target, baseline included),
`--no-llm` (rules only), `--report <path>`. Ctrl-C stops cleanly: the running
TradeFed process group is killed and every finished iteration is already saved
in `results/fixloop/sessions/<id>.json`.

## Outcomes

| Status | Meaning |
|---|---|
| `FIXED_IN_CODE` | Green on a rebuilt, reflashed image with an OEM source patch, and green again in a confirmation run. The patch is saved for review. |
| `FIXED` | Green after one or more device/host remediations (lab setup), confirmed. |
| `WORKAROUND_VERIFIED` | Green only with a device-side workaround that proves a **product** root cause. The page shows the permanent fix that must ship in OEM code/config. Not a fix for certification. |
| `FLAKY` | Failed, then passed twice on unchanged re-runs. |
| `NEEDS_CODE_FIX` | No remediation helped and the failure points at product code. The page shows the LLM root cause and the OEM code it found. |
| `NO_FIX_FOUND` | Not product code, but nothing in the catalog helped. |
| `ALREADY_PASSING`, `DEVICE_LOST`, `ABORTED`, `ERROR` | As named. A lost DUT is first reconnected, then `fix_loop.restart_command` is tried. |

## Safety model

* **Allow-listed actions only.** The loop applies remediations from
  `config/remediations.yaml` and nothing else. Review that file like code.
* **The LLM chooses, it does not act.** It may name catalog ids; unknown ids
  are dropped. It never supplies shell commands.
* **Values from failure text are checked.** `{m1}` captures must match a
  package-name pattern before they reach `adb shell`.
* **Probe before acting.** A remediation with `detect`/`broken_if` is used only
  if the live device really is in that broken state.
* **Host untouched.** Host-side fixes (e.g. a newer `aapt2`) only change the
  PATH of the TradeFed process the loop starts.
* **CTS is never edited.** Product problems end as `NEEDS_CODE_FIX` or
  `WORKAROUND_VERIFIED`; the loop does not patch tests to make them pass.
* **Local LLM.** The loop uses the `openai_compatible` endpoint from
  `fix_loop.llm_*` or `ai_rca`; external endpoints are refused unless
  `ai_rca.allow_external_providers: true`.

## OEM code (local folder or MCP)

OEMs change CarLauncher, CarSettings, CarSystemUI, dialers, HALs and overlays,
and many xTS failures come from those changes. Point the loop at that code in
`fix_loop.oem_sources`:

```yaml
fix_loop:
  oem_sources:
    - name: OemLauncher
      kind: local
      path: /src/oem/packages/apps/OemLauncher
      packages: [com.oem.launcher]          # searched first when this package is in the failure
    - name: oem-code-search
      kind: mcp
      command: /opt/oem/bin/code-search-mcp --repo-root /src/oem
      search_tool: search_code              # called as {"query": "<term>", "limit": 2}
      packages: [com.oem.settings, com.oem.systemui]
```

For each diagnosis the loop builds search terms from the failure (component
names, resource ids, classes in the stack) plus up to four identifiers the LLM
proposes (e.g. `config_defaultDialer`), searches the implicated sources first,
and gives the matching code windows to the LLM. The page lists the files and
lines consulted.

`kind: mcp` starts the server over stdio and calls its search tool with the
standard MCP `tools/call` request; any MCP server that returns text results
works. Nothing is sent anywhere except to that server.

## Writing remediations

```yaml
- id: device_connect_lab_wifi
  title: DUT has no usable network; join the lab Wi-Fi
  category: ENVIRONMENT_ISSUE            # also INFRASTRUCTURE_FAILURE, PRODUCT_BUG
  scope: device                          # device | host_tool | reboot
  match: ["UnknownHostException", "No valid test network"]
  detect: "cmd wifi status | head -3"    # read-only
  broken_if: "Wifi is disabled|not connected"
  apply: ["cmd wifi set-wifi-enabled enabled",
          "sleep 8; cmd wifi connect-network {var.lab_wifi_ssid} {var.lab_wifi_security}"]
  revert: ["cmd wifi set-wifi-enabled disabled"]
  why: "..."                             # shown on the page
  prep_hint: "..."                       # what to make permanent in lab setup
```

Lab-specific values (`{var.NAME}`) come from `fix_loop.variables`. Set
`workaround: true` plus `permanent_fix:` for device-side changes that only
prove a product root cause.

## Limitations

* Code fixes need a build tree and a way to flash the DUT on the host the loop
  runs on; builds are serialized (one tree). Patches are proposals verified by
  CTS on one DUT: they still need the owning team's review.
* One target runs on one DUT at a time. Tests that need several DUTs
  (multi-device CTS) are not supported.
* The LLM's confidence is self-reported and is not an accuracy measure.
