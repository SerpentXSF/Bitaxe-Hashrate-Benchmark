# Error-aware Bitaxe benchmark — design

## Problem

The upstream benchmark (mrv777) sweeps core voltage × frequency and picks the
setting with the **highest hashrate**, reporting J/TH efficiency separately. It
never looks at the ASIC **hardware error rate**. On BM1370 boards an aggressive
undervolt can look great on hashrate and J/TH while the ASIC throws 10–20%
hardware errors — the tool will happily select that point. Field example: a
Gamma running 1050 mV / 575 MHz benched as "best" (15.2 J/TH) yet sustained
~10–15% error.

Goal: choose settings that are efficient **and** low-error, and add a fast mode
for rescuing a single troubled ASIC.

## Approach

Keep it a **single file**, a strict backward-compatible superset of upstream, so
the change stays a clean drop-in that mrv777 could adopt. Separate the decision
logic into **pure functions** (no globals, no I/O) so it is unit-testable
without hardware; keep the HTTP/device code thin around it. Wrap the run in
`main()` / `if __name__ == "__main__"` so the module can be imported by tests
without executing a benchmark.

### Error measurement (reset-aware)

`/api/system/info` exposes both `errorPercentage` and
`hashrateMonitor.asics[].errorCount`. **Live probing on real BM1370 firmware
showed these are two different kinds of signal:**

- `errorCount` is a **cumulative** raw counter (monotonically increasing).
- `errorPercentage` is a **noisy rolling/instantaneous** rate — it swings
  sample-to-sample (e.g. 14% → 10% → 19% over 10 s) and is *not* a cumulative
  ratio. A "derive total work = errorCount / errorPercentage then take a delta"
  approach is therefore invalid (the derived denominator moves backwards).

So the per-combo error metric is the **mean of `errorPercentage` over the
post-warmup samples** of the 10-minute window. Averaging ~34 samples smooths the
rolling-rate noise and reproduces the figure AxeOS shows. Reset-awareness comes
from (a) excluding the warmup samples after each reboot and (b) each combo
running from a fresh reboot. If no `errorPercentage` data is present (older
firmware), report `None` (unknown) and do not disqualify the combo.

Separately, the **raw `errorCount` delta over the fixed-length window**
(`errorCountDelta`) is recorded as a directly-comparable error-volume
diagnostic (same window length for every combo). It is informational; the gate
uses the mean-percentage metric.

### Winner selection: error-gate → efficiency

- A combo **passes** the gate when its error% is unknown (`None`) or `<=`
  `--max-error` (default **3.5%**).
- Best = **lowest J/TH among passers**; ties broken by higher hashrate.
- If no combo passes, fall back to the lowest-error combo and warn.
- Reported rankings still include top-by-hashrate and top-by-J/TH, plus a new
  top-by-lowest-error, each flagged pass/fail.

### Modes

- `--mode grid` (default): the existing full sweep, now error-aware. A combo
  that fails the gate is treated like an unstable result (raise voltage / step
  back frequency) instead of climbing.
- `--mode refine`: the single-ASIC rescue path. Start at the device's current
  (or `-v`/`-f`) setting and sweep **voltage upward** to the first setting that
  passes the gate with in-tolerance hashrate, then probe **downward** for a
  leaner (lower J/TH) passer. If the frequency is **thermally boxed in** — the
  temperature ceiling is hit before the error clears — automatically **drop the
  frequency** by one increment and retry, because frequency (not voltage) is the
  lever once cooling is the limit. Combos whose best-case mean can no longer
  reach the ceiling are aborted early. Minutes, not hours.

  (Field note: on real BM1370 Gammas, both troubled miners and the 1.5 Ths
  overclock unit turned out to be thermally boxed in at their aggressive clocks —
  error fell monotonically with voltage but hit the temp ceiling first. The
  frequency-drop behavior exists so refine handles that case on its own.)

### Output

- JSON (unchanged shape) gains `errorRate` and `passedErrorGate` per result.
- New CSV `bitaxe_benchmark_results_<ip>_<ts>.csv` and a human-readable ranked
  table (error%, J/TH, hash, temps, pass/fail).

### Resume

`--resume` reloads the existing results file for the IP and skips already-tested
(voltage, frequency) combos.

### CLI (all additive; legacy invocations behave the same, plus error is shown)

```
--max-error <pct>     error ceiling, default 3.5
--mode {grid,refine}  default grid
--resume              skip already-tested combos
--no-error-gate       disable gating (report error as info only = legacy pick logic minus metric)
--benchmark-time <s>  override per-combo window (default 600) — useful for quick validation
```

Existing `-v` / `-f` unchanged. In grid mode, omitted `-v`/`-f` keep the upstream
1150 mV / 500 MHz start; in refine mode they default to the device's current
voltage/frequency.

## Safety

All existing safeties preserved unchanged: chip-temp cutoff, VR-temp cutoff,
input-voltage window, power cutoff, min-sample count, best-settings restore on
exit/Ctrl-C, voltage/frequency clamps.

## Testing

Pure functions unit-tested with synthetic samples (no hardware):
`compute_window_error`, `passes_error_gate`, `select_best`, and the refine
stop decision. Hardware validation: refine-mode run on a troubled Gamma
(.197), confirm the selected point drops error under the ceiling at good J/TH,
then repeat on a second device before committing.

## Attribution / license

GPLv3 preserved. Upstream (mrv777) credited in the README; changes documented so
the diff is a clean PR candidate.
