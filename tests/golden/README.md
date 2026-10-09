# Golden fixtures

Real TradeFed output from this lab (CTS 17_r2, Cuttlefish `aosp_cf_x86_64_auto`,
`0.0.0.0:65xx` devices), gzipped. Used by `tests/test_golden.py`.

| File | Source | Notes |
|------|--------|-------|
| `cts_failures_subset.xml.gz` | `/opt/xts/android-cts/results/2026.10.05_21.11.12.345_5909/test_result.xml` (82 MB, 1251 modules, 1303 failures) | 15 `<Module>` elements kept verbatim: 10 with failures (incl. an `[instant]` pair), 3 interrupted (`done="false"` with `<Reason>`), 2 passing. `Build`/`RunHistory` unchanged; `<Summary>` recomputed for the subset. |
| `bionic_result.xml.gz` | `/opt/xts/android-cts/results/2026.10.06_01.02.22.903_1303/test_result.xml` | Unmodified. One module, 3275 passes. |
| `bionic_console.log.gz` | `results/logs/tradefed_run_1791216140.log` | Unmodified console output of that run (unsharded). |

Expected values in the tests were counted with ElementTree/grep on these
files, not with the agent's parser. The fixtures contain the lab's build
fingerprints and paths; replace them with your own runs' output if that
matters for where this repo goes.

To add a case: take a real `test_result.xml` or console log that broke
something, cut it down (keep elements verbatim), gzip it with a fixed
mtime (`gzip.GzipFile(path, "wb", 9, mtime=0)`), and assert what TradeFed
itself reports for it.
