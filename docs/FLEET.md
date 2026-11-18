# Fleet: one master console for several lab PCs

Status: experimental, on the `feature/fleet-master` branch.

Each lab PC keeps its own agent, suite packages, JDK and USB devices, and runs
`xts-agent ui`. One of them, the **master**, lists the others as **workers**
and controls them over the HTTP API every console already has. TradeFed always
runs on the PC that holds the devices; nothing forwards adb over the network.

## Set up

On every worker PC (same agent version and suite packages as the master):

```bash
export XTS_UI_TOKEN=$(openssl rand -hex 16)      # keep it; the master needs it
xts-agent ui --host <this PC's LAN address> --port 8765
```

On the master: **Settings → Fleet workers → Add a worker PC**, with a name,
the worker's URL (`http://192.168.1.22:8765`) and its token, then Save. Tokens
are written to `config/local_settings.yaml` (mode 600, git-ignored) and are
never sent back to the browser. To include the master's own devices in a
split, add it as a worker too (`http://127.0.0.1:8765`, no token).

## What the master can do

| Where | What |
|---|---|
| **Fleet** page | Every worker: online or not, devices, free devices, build, running plans, with Log and Stop |
| **Launch → Run on** | Start a plan on a chosen worker. Device and module lists come from that worker |
| **Fleet → Split a suite across PCs** | Divide one suite's modules across workers, preview, start, and follow one merged run |

A plan started through the master is the master's plan file. It is saved on
the worker as `fleet-<name>.yaml` and launched there, so plans are maintained
in one place.

## How a split works

1. The master reads each selected worker's devices. A worker needs at least
   one free, healthy device. All free devices across the selected workers must
   run **one build fingerprint**, or the split is refused.
2. The module list is the plan's `include_filters` if it has them, otherwise
   the suite's own `list modules` from the workers. All workers must have the
   **same suite build**.
3. Modules are dealt out in proportion to each worker's free devices: a PC
   with four devices gets about twice the modules of one with two. Balancing is
   by module count, not by measured duration.
4. Each worker gets the plan narrowed to its share and runs it as a normal
   local run: sharded over its own devices, with its own retries.
5. The master records the parts in `results/fleet_runs/<id>.json` and shows
   one merged row: state, status and pass/fail summed over the parts.

`Stop all` stops every running part. If the master goes down, the parts keep
running; when it is back it reads their state again.

## Limits

- **One result per PC.** A split produces one `test_result.xml` and one
  submission zip per worker, and each part runs with the development profile
  (a filtered certification plan is refused by design). Keep a submission run
  on one PC until Google confirms split results are acceptable for your case.
- **No automatic re-dispatch.** If a worker dies mid-run, its share is not
  moved to another PC; start that share again by hand.
- **No queue.** A worker without a free device is refused, not queued.
- **Triage is per worker.** Reports and interactive triage for a part live on
  the worker that ran it (open its console). History, verdicts and the AI
  cache are shared only when every PC uses the same PostgreSQL
  (`XTS_DATABASE_URL`).
- **No TLS.** With `http://` worker URLs the token crosses the LAN in clear
  text. Use a trusted lab network, an SSH tunnel or a TLS reverse proxy.
- Through the master only these worker calls are reachable: overview,
  devices, live runs, run history and detail, logs, plans, module lists,
  launch and stop. Worker settings and files are not.

## API (master)

| Call | Purpose |
|---|---|
| `GET /api/fleet` | Workers with devices and live runs |
| `GET /api/fleet/w/<worker>/api/...` | Allow-listed pass-through to one worker |
| `POST /api/fleet/launch` | `{worker, plan, ...launch options}` |
| `POST /api/fleet/split` | `{plan, workers?, suite?, modules?, serials?, preview?, dry_run?, auto_retry?}` |
| `GET /api/fleet/runs`, `GET /api/fleet/runs/<id>` | Merged fleet runs |
| `POST /api/fleet/runs/<id>/stop` | Stop every running part |

`serials` in a split is `{worker: [serial, ...]}` and restricts that worker's
part to those devices. It exists mainly to try a split on one machine, where
two worker consoles see the same devices.
