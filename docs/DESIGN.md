# Design notes: error-aware Bitaxe benchmark

## Why

The upstream benchmark sweeps core voltage and frequency and selects the setting
with the highest hashrate, reporting J/TH separately. It does not look at the
ASIC hardware error rate. On BM1370 boards an aggressive undervolt can look good
on hashrate and J/TH while the ASIC reports a double-digit hardware error rate,
and the upstream tool will select that point. The goal here is to pick settings
that are both efficient and low-error, and to add a fast mode for stabilizing a
single unstable ASIC.

## Structure

The tool stays a single file and a backward-compatible superset of upstream, so
it remains an easy drop-in. The decision logic is separated into pure functions
(no globals, no I/O) so it can be unit-tested without hardware, with the
HTTP/device code kept thin around it. The run is wrapped in `main()` /
`if __name__ == "__main__"` so the module can be imported by tests without
executing a benchmark. `benchmark_iteration` returns a result dict rather than a
positional tuple, so adding a field can't shift an unpack.

## Error measurement

`/api/system/info` exposes both `errorPercentage` and
`hashrateMonitor.asics[].errorCount`, and on BM1370 firmware these are two
different kinds of signal:

- `errorCount` is a cumulative raw counter (monotonically increasing).
- `errorPercentage` is a noisy rolling rate. It swings from sample to sample and
  is not a cumulative ratio, so deriving total work as
  `errorCount / errorPercentage` and taking a delta is invalid (the derived
  denominator can move backwards).

So the per-combo error metric is a mean of `errorPercentage` over the post-warmup
samples of the window, which reproduces the figure AxeOS reports. With enough
samples it uses a light trimmed mean (dropping one extreme at each end) so a
single noisy sample can't flip a borderline result, and the spread (std/min/max)
is recorded. The raw `errorCount` delta over the fixed-length window
(`errorCountDelta`) is recorded separately as a comparable measure of error
volume. If a device exposes no error data, the error rate is reported as unknown
and never disqualifies a combination.

## Winner selection

A combination passes the gate when its error rate is unknown or at or below
`--max-error` (default 3.5%). The best setting is the lowest J/TH among passers
that also held hashrate in tolerance, with ties broken by higher hashrate. If no
combination passes, the tool falls back to the lowest-error result. The reported
rankings include top-by-hashrate, top-by-J/TH, and top-by-lowest-error, each
flagged pass/fail.

## Modes

- `grid` (default): the full sweep, now error-aware. A combination that fails the
  gate is treated like an unstable result (raise voltage, step frequency back)
  instead of climbing; a thermally-capped combination retreats the same way.
- `refine`: stabilize a single ASIC. Start at the device's current (or `-v`/`-f`)
  setting and sweep voltage up to the first setting that passes the gate with
  in-tolerance hashrate, then probe down for a lower-voltage passer. If the chip
  hits the temperature ceiling before the error clears, drop the frequency by one
  increment and retry, since frequency is the effective lever once cooling is the
  limit. Combinations whose best-case mean can no longer reach the ceiling are
  stopped early but still recorded.
- `efficiency`: for a healthy miner. Hold the frequency and sweep voltage down
  from the current setting to the lowest voltage that still clears the ceiling,
  cutting power and heat with no loss of hashrate.
- `--check`: read-only. Measure the current setting once and report; no changes,
  no reboot.

## Robustness

Settings are read back and confirmed after each apply, so a failed PATCH or a
watchdog reboot into different settings can't mislabel a result. A dropped
request retries once instead of ending the run. `--resume` reloads the most
recent results file for the IP (matched by glob so it survives an hour boundary)
and replays the recorded pass/fail branch for already-tested combinations. The
best setting (or the original) is restored on exit and on Ctrl+C.

## Output

The JSON keeps the upstream shape and adds `errorRate`, `errorRateStd`,
`errorCountDelta`, `passedErrorGate`, `hashrateWithinTolerance`, and
`earlyAborted` per result. A CSV and a ranked console summary are written
alongside it.

## Compatibility

All existing safeties are preserved: chip-temp and VR-temp cutoffs, the
input-voltage window, the power cutoff, the minimum sample count, and the
voltage/frequency clamps. Existing invocations behave as before, with the error
rate now also reported. GPLv3 and upstream attribution are preserved.
