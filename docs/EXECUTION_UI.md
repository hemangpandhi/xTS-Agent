# Running and watching xTS executions

This page is the day-to-day process for running xTS with the agent and the web
console you use to start runs, watch them live and review results. For first-time
host setup and the validation stages, see [EXECUTION_GUIDE.md](EXECUTION_GUIDE.md).

## The process

| Step | What you do | Where | How you know it worked |
|------|-------------|-------|------------------------|
| 1. Host ready | `scripts/preflight.sh` (once per host, and after upgrades) | shell | All checks green |
| 2. Devices up | Connect the rack, or start Cuttlefish: `scripts/start_cluster.sh 6` | shell | **Devices** page: every DUT `online`, same build, `free` |
| 3. Local LLM up (only for AI RCA) | `PORT=8011 /mnt/OEM/llm-env/serve_qwen.sh` | shell | `curl -s 127.0.0.1:8011/v1/models` lists `coder` |
| 4. Open the console | `xts-agent ui` and browse to <http://127.0.0.1:8765/> | browser | The pages load |
| 4a. First time on a host: Settings | **Settings**: unzip each suite, **Scan** its folder, **Use for x86_64/arm64**, check JDK, SDK and adb, test the LLM, **Save** | console | Every suite you need shows a green build badge for your device architecture |
| 5. Launch | **Launch**: pick a plan and the device architecture, optionally pick modules from the suite's own list, tick auto-retry, **Start run**. Reusable selections: **Plans → New plan** | console (or `xts-agent run --plan …` in a shell, or CI) | **Live** shows the run within seconds |
| 6. Watch | **Live**: phase, per-suite status, the module each device is running, failures so far, TradeFed console | console | Heartbeat stays fresh; "Log quiet" stays low |
| 7. Stop or resume if needed | **Stop** on the run card, later **Launch → Resume** (or `run --resume`) | console | Card shows `CANCELLED`; resume continues from the partial result |
| 8. Review | **Runs → run**: totals, module table, failed tests, links to the **Official Google report** (`test_result_failures_suite.html` / `.zip`), agent HTML report and **Interactive triage** | console | Triage groups failures deterministically by signature first, with owners and optional AI root cause |
| 9. Act & Discuss | **Triage**: inspect deterministic root-cause groups, discuss with grounded local AI or add team notes, save human verdicts, promote signatures to `config/known_issues.yaml`, or 1-click re-run a group's failing tests | console / Jira | Next run's Level-1 triage recognizes promoted signatures deterministically |

Every run, however it was started, writes `results/run_state/<plan>.json`
while it runs and archives it to `results/run_state/history/<run_id>.json` when
it ends. The console only reads those files, the results database and TradeFed's
`test_result.xml`, so:

- a run started from a shell or from CI shows up on **Live** too;
- closing the browser, or stopping and restarting `xts-agent ui`, never stops a run;
- runs started before the console existed appear under **Runs** from the database.

## The console

```bash
xts-agent ui                       # http://127.0.0.1:8765/ on this host only
xts-agent ui --port 9000
XTS_UI_TOKEN=$(openssl rand -hex 16) xts-agent ui --host 0.0.0.0   # lab network; open http://<host>:8765/#token=<token>
```

From another machine without opening a port, use an SSH tunnel instead:
`ssh -L 8765:127.0.0.1:8765 lab-host`, then browse to <http://127.0.0.1:8765/>.

### Live

One card per running plan:

- **Phase**: Execute, RCA, Triage, Reports, Done. A run that dies without
  finishing shows `INTERRUPTED` with the resume hint.
- **Suites**: status (`RUNNING`, `RETRYING` with the attempt number, `PASSED`,
  `FAILED`, `INCOMPLETE`), modules finished and started, test failures so far,
  how long TradeFed's log has been quiet (amber after 15 minutes: a likely hang),
  elapsed time.
- **Devices**: one tile per allocated device with the module it is running now.
- **TradeFed console**: the live log of the selected suite, with failures
  highlighted, a "failures only" filter and follow mode.
- **Timeline**: started, retry attempts, suite results, phases.
- **Stop** sends the same SIGTERM as a CI cancel: TradeFed writes a partial
  `test_result.xml` and the run can be resumed. **Force stop** sends the second
  signal: TradeFed is killed and the agent exits at once.

The status refreshes every `ops.status_interval_secs` (15 seconds by default);
the progress line in the job log stays at `ops.progress_interval_secs`.

### Launch

Lists every plan in `config/test_plans/`. Options:

- **Device architecture**: picks the suite packages (see Settings).
- **Shards / device count & Pin specific device(s)**: choose `Plan default`,
  `1 device (single DUT, no sharding)`, `All free devices (auto)`, or a fixed/custom
  shard count, and optionally tick specific attached ADB serials under **3 · Devices**
  (`devices.serials`) to pin execution to exact DUTs.
- **Run only some modules**: the module list comes from the installed suite
  itself (`<suite>-tradefed list modules`, about 4 seconds, then cached per
  suite build; **Refresh** runs it again). Search it, tick modules, or use
  **Select all shown** on a search such as `Car`. Each module shows its ABIs and
  parameterised variants (secondary user, instant app). For a single test class
  or method, or one ABI, open **Test classes, methods or one ABI…** and write a
  TradeFed include filter per line, e.g.
  `CtsCarTestCases android.car.cts.CarPropertyManagerTest#testGetProperty` or
  `x86_64 CtsBionicTestCases`. With several suites in a plan, only the suites you
  pick modules for run. The console writes a derived plan to
  `results/ui_plans/` and runs that; the original plan is never changed. A
  certification plan narrowed this way runs as a development run.
- **Save selection as a new plan** opens the plan editor with the plan, the
  modules and the options you chose, ready to name and save.
- **Auto-retry**, **Resume**, **Dry run**, and **AI RCA** on or off.
- The command it will run is shown before you start.

A plan that is already running cannot be started twice. Device sharing between
different plans is prevented by the agent's leases, as for CLI runs.

### Plans

Every plan in `config/test_plans/`, built-in and your own.

- **New plan**: a name, a file name (`car_nightly` becomes
  `config/test_plans/car_nightly.yaml`), description, profile, architecture,
  minimum devices and AI root-cause analysis, then one or more suites. For each
  suite: the TradeFed plan (suggestions come from `<suite>-tradefed list plans`),
  shards, retries, timeout, the modules to include (same picker as Launch) and
  modules to exclude.
- **YAML** tab: the file exactly as it will be saved. Edit it directly, or
  **Load file…** to bring in a plan file from your computer.
- **Validate** loads the plan the way a run does and shows each suite's package
  path; **Save plan** validates first, so a saved plan always loads.
- Plans you create start with `# created by xts-agent ui`; only those can be
  overwritten or deleted from the console. A built-in plan is read-only here:
  **Duplicate as custom plan** and change the copy.
- A saved plan appears on Launch at once and runs from the CLI and CI as well:
  `xts-agent run --plan config/test_plans/car_nightly.yaml`.

### Runs and run detail

**Runs** lists finished, cancelled and interrupted runs with totals, duration,
device count and links to the HTML report and triage page. Opening a run shows:

- totals, duration, devices and triage group count;
- the timeline;
- per suite, the module table from `test_result.xml` (passed, failed, skipped,
  total, done or incomplete, runtime, reason), filterable to modules with
  failures or incomplete modules and sortable;
- the failed tests with their first message line (the first 300; the triage page
  has all of them, grouped);
- **Export JSON** and **Export modules CSV**;
- **Official Google / TradeFed reports**: opens TradeFed's native
  `test_result_failures_suite.html` (with `compatibility_result.css` and `logo.png`),
  `test_result.xml`, and streams the official `<session>.zip` submission archive.

A full CTS result takes a few seconds to read the first time it is opened, then
it is cached.

### Interactive Triage & Discussion (`#/triage`)

A two-level triage workspace:

1. **Level 1 (always deterministic)**: collapses failing tests into ranked
   root-cause groups by normalised signature, applies regex rules, matches
   `config/known_issues.yaml` waivers and `config/ownership.yaml` teams, and
   computes `NEW` / `PERSISTENT` / `FLAKY` history labels—computing directly from
   `test_result.xml` on the fly if no `triage_*.json` file was written.
2. **Level 2 (on-demand AI & team discussion)**: per root-cause group, inspect
   numbered AOSP/OEM source windows and `device_logcat_test_*.txt` slices, run
   on-demand structured AI RCA, chat multi-turn with the configured LLM (or leave
   offline team notes) persisted by `signature` across runs, click **Copy prompt
   (ChatGPT)** to copy a self-contained grounded prompt for any browser-based AI
   chat without needing an API key, record human verdicts, **re-run just that
   group's failing tests**, or **promote the signature to
   `config/known_issues.yaml`** as a deterministic Level-1 rule.

### Devices

What `adb` sees on the host: state, model, build type, fingerprint, which
process holds each device (`pid`, user, CI job), and the quarantine list with a
**Release** button, plus each device's CPU ABI.

### Settings

Everything a host needs before its first run, saved to
`config/local_settings.yaml` (git-ignored) and merged over
`config/default_config.yaml` for every run, whether started from the console,
a shell or CI. Runs already in progress keep the settings they started with.

- **Test suites**: one path per suite (CTS, VTS, STS, GTS, ATS, CATBox) and per
  device architecture. Google ships separate x86 and ARM suite downloads that
  cannot be mixed, so a lab with Cuttlefish and real head units keeps two sets,
  for example `/opt/xts/x86_64/android-cts` and `/opt/xts/arm64/android-cts`.
  Each path is checked as you type: build number, the ABIs it contains, module
  count, and a warning if an x86 package sits in the arm64 column. An empty cell
  uses `<suite packages folder>/android-<suite>` and shows what that folder holds.
  **Default architecture** is used when a plan or launch does not choose one.
- **Find suite packages** scans a folder (three levels deep) for suite packages
  and fills a cell with **Use for x86_64 / arm64**.
- **Host tools**: suite packages folder, JDK, Android SDK (`aapt2`) and `adb`,
  each validated.
- **AI root-cause analysis (optional)**: choose any of four modes:
  - **Local Server (`llama.cpp` `llama-server`, `Ollama`, `vLLM`, `LM Studio`)** —
    1-click presets (`:8080`, `:11434`, `:8011`, `:1234`) plus **Test** to verify
    reachability and list models; logs and source never leave your network.
  - **`llama.cpp` GGUF Path (`provider: llama_cpp`)** — direct `.gguf` file path
    loaded in-process via `llama-cpp-python` (cached across interactive triage
    questions), with live file/size/package validation, `n_ctx` and `n_gpu_layers`.
  - **Online API (`ChatGPT / OpenAI`, `Google Gemini`, `OpenRouter`)** — requires
    ticking **Allow sending failure context to this external AI API** and setting
    `XTS_LLM_API_KEY` or `XTS_GEMINI_API_KEY` in the server environment (API keys
    are never written to YAML).
  - **Disabled (`provider: none`)** — 100% deterministic Level-1 triage, history,
    waivers, verdicts, team notes, and 1-click **Copy prompt for ChatGPT** with
    zero LLM calls.

A suite's package path is chosen in this order: a `package_path` written in the
plan itself, then the Settings path for the run's architecture, then the
default folder. A plan can name its architecture with `arch: arm64`; **Launch**
can override it per run. Launch shows, per suite, the path the run will use and
whether the package is installed, refuses to start while one is missing, and
warns when the online devices are a different architecture.

From the command line the same file applies; set `XTS_LOCAL_SETTINGS` to use
another one.

## Security

The console can start and stop runs on lab devices, so:

- it listens on `127.0.0.1` only unless you pass `--host`, and refuses any other
  address without a token (`--token` or `XTS_UI_TOKEN`);
- with a token, every API call needs `Authorization: Bearer <token>`; the page
  takes it once from `#token=` and keeps it for the browser tab;
- start, stop and release need an `X-XTS-UI` header, which a cross-site form
  cannot send, and on loopback a foreign `Host` header is refused (DNS rebinding);
- launch only accepts plan files from `config/test_plans/` and module names from a
  strict pattern, and builds a fixed `xts-agent run` command (no shell);
- stop only signals a process id recorded in a run-state file whose command line
  is the agent;
- `/files/` serves only report-type files under `results/`, and HTML reports are
  sandboxed.

There are no user accounts. Put it behind your SSO reverse proxy if more than
the lab team can reach the host.

## What came from the TestDeviceFarm project

The console takes the execution views of the TestDeviceFarm web UI (xTS test
page, test history, result detail, live output) and rebuilds them on the agent's
own data. Different by design:

| TestDeviceFarm | xTS Agent console |
|----------------|-------------------|
| FastAPI central server, per-host node agents, React/Vite build | One `xts-agent ui` process, standard library only, single HTML page |
| Runs one TradeFed command per device, parsing stdout for progress | Runs agent plans: health gate, leases, sharding across devices, retry, triage, AI RCA |
| Progress guessed from stdout text | Per-device module progress from the agent's TradeFed log parser |
| Results kept in memory of the node agent | Run-state files, archive, results database, `test_result.xml` |
| Device screen streaming, flashing, hard keys, login | Not included: out of scope for xTS execution |

Not carried over: device screen streaming (scrcpy/WebRTC), flashing, hardware
keys, user accounts and multi-host node registration.
