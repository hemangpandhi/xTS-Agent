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
| 4. Open the console | `xts-agent ui` and browse to <http://127.0.0.1:8765/> | browser | The four tabs load |
| 5. Launch | **Launch**: pick a plan, optionally list modules, tick auto-retry, **Start run** | console (or `xts-agent run --plan …` in a shell, or CI) | **Live** shows the run within seconds |
| 6. Watch | **Live**: phase, per-suite status, the module each device is running, failures so far, TradeFed console | console | Heartbeat stays fresh; "Log quiet" stays low |
| 7. Stop or resume if needed | **Stop** on the run card, later **Launch → Resume** (or `run --resume`) | console | Card shows `CANCELLED`; resume continues from the partial result |
| 8. Review | **Runs → run**: totals, module table, failed tests, links to the HTML report and the **triage page** | console | Triage page groups the failures with owners and AI root cause |
| 9. Act | File or update Jira from the triage page (dry-run first), fix lab issues, rerun the failing modules from **Launch** | console / Jira | Next run's triage labels the group `PERSISTENT`, `FLAKY` or gone |

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

- **Only these modules**: one per line, as TradeFed include filters
  (`CtsBionicTestCases`, or `CtsFoo android.foo.BarTest#testX`). The console
  writes a derived plan to `results/ui_plans/` and runs that; the original plan
  file is never changed. A certification plan narrowed this way runs as a
  development run, because certification refuses filtered plans.
- **Auto-retry**, **Resume**, **Dry run**, and **AI RCA** on or off.
- The command it will run is shown before you start.

A plan that is already running cannot be started twice. Device sharing between
different plans is prevented by the agent's leases, as for CLI runs.

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
- **Export JSON** and **Export modules CSV**.

A full CTS result takes a few seconds to read the first time it is opened, then
it is cached.

### Devices

What `adb` sees on the host: state, model, build type, fingerprint, which
process holds each device (`pid`, user, CI job), and the quarantine list with a
**Release** button.

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
