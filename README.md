# Bitaxe Hashrate Benchmark (error-aware)

A Python-based benchmarking tool for optimizing Bitaxe mining performance by testing different voltage and frequency combinations while monitoring hashrate, temperature, power efficiency, and ASIC hardware error rate.

This is an error-aware fork of [mrv777/Bitaxe-Hashrate-Benchmark](https://github.com/mrv777/Bitaxe-Hashrate-Benchmark). The upstream tool selects the setting with the highest hashrate and reports J/TH separately; it does not look at the ASIC error rate. On BM1370 boards an aggressive undervolt can look good on hashrate and efficiency while the chip throws double-digit hardware errors, and that is the setting the original picks. This fork measures the error rate for each combination, rejects settings above a configurable ceiling, and selects the most efficient setting that stays under it.

## What this fork adds

- **Error-rate measurement**: a trimmed mean of `errorPercentage` over each combo's stable window (with `std`/`min`/`max` recorded), plus the raw ASIC error count for the window (`errorCountDelta`).
- **Error gate, then efficiency**: the best setting is the lowest J/TH among combinations that stay within the error ceiling (`--max-error`, default 3.5%), not the raw fastest. It falls back to the lowest-error setting when nothing clears the ceiling.
- **`refine` mode**: rescue an unstable ASIC. It sweeps voltage up to the lowest setting that clears the ceiling, then probes down for a lower-voltage setting that still passes (better J/TH). If the chip hits the temperature ceiling before the error clears, it lowers the frequency and retries, since frequency is the effective lever once cooling is the limit. Combinations that clearly can't clear the ceiling are stopped early (and still recorded) to save time.
- **`efficiency` mode**: for a healthy miner. It holds the frequency and lowers voltage from the current setting to the lowest voltage that still clears the ceiling, cutting power and heat with no loss of hashrate.
- **Robustness**: settings are read back and confirmed after each apply, so a failed PATCH or watchdog reboot can't mislabel a result. A dropped request retries once instead of ending the run. The gate uses a light trimmed mean and records the spread (`std`/`min`/`max`) so one noisy sample won't flip a borderline result.
- **Configurable chip-temp cutoff** (`--max-temp`): BM1370/Gamma boards often idle near 65°C and need 68 to leave room to raise voltage.
- **CSV export and a ranked summary** (highest hashrate, most efficient, lowest error) alongside the existing JSON.
- **Resume** (`--resume`): reload prior results for an IP and skip already-tested combinations.
- The decision logic has unit tests (see `tests/`), and the run is wrapped in `main()` so the module can be imported without running a benchmark.

Existing commands work exactly as before; the error rate is now also reported.

## Which mode should I use?

| Your situation | Use | What it does |
|---|---|---|
| Quick health snapshot, no changes | `--check` | Measures the current setting once (no reboot) and reports error / J-TH / temp |
| Fresh chip, or you want the full voltage x frequency picture | `--mode grid` (default) | Full sweep; picks the most efficient setting under the error ceiling |
| Miner is erroring or unstable and you want it fixed | `--mode refine` | Sweeps voltage up (and lowers frequency if it overheats) until the error clears |
| Miner is already healthy and you want less power/heat | `--mode efficiency` | Holds the frequency and trims voltage down to the leanest setting that still passes |

`refine` and `efficiency` start from the device's current setting; `grid` starts from a conservative default and climbs. Every mode restores the best setting it found (or your original) on exit, including on Ctrl+C.

## Prerequisites

- Python 3.11 or higher
- Access to a Bitaxe miner on your network
- Docker (optional, for containerized deployment)
- Git (optional, for cloning the repository)

## Installation

### Standard Installation

1. Clone the repository:
```bash
git clone https://github.com/SerpentXSF/Bitaxe-Hashrate-Benchmark.git
cd Bitaxe-Hashrate-Benchmark
```

2. Create and activate a virtual environment:
```bash
python -m venv venv
# On Windows
venv\Scripts\activate
# On Linux/Mac
source venv/bin/activate
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

### Docker Installation

1. Build the Docker image:
```bash
docker build -t bitaxe-benchmark .
```

## Usage

### Standard Usage

Run the benchmark tool by providing your Bitaxe's IP address:

```bash
python bitaxe_hashrate_benchmark.py <bitaxe_ip>
```

Optional parameters:
- `-v, --voltage`: Initial voltage in mV (grid default: 1150; refine/efficiency default: device current)
- `-f, --frequency`: Initial frequency in MHz (grid default: 500; refine/efficiency default: device current)
- `--mode {grid,refine,efficiency}`: full sweep (default), rescue an unstable ASIC, or trim voltage down on a healthy one
- `--max-error <pct>`: error-rate ceiling for selecting the best setting (default: 3.5)
- `--max-temp <C>`: chip temperature cutoff (default: 66; BM1370/Gamma often need 68)
- `--no-error-gate`: report the error rate but do not use it to gate selection
- `--resume`: reload prior results for this IP and skip already-tested combos
- `--benchmark-time <s>`: override the per-combo window in seconds (default: 600)
- `--voltage-step <mV>` / `--frequency-step <MHz>`: sweep increments (defaults: 20 / 25)
- `--dry-run`: print the resolved plan and exit without touching the device
- `--check`: read-only. Measure the current setting once (no changes, no reboot) and report. Uses a shorter ~240s window unless `--benchmark-time` is given.

Quick health check (no changes to the miner). On a Gamma that idles near 66-67 C, pass `--max-temp 68` so the check isn't cut short by the temperature cutoff:
```bash
python bitaxe_hashrate_benchmark.py 192.168.2.29 --check --max-temp 68
```

Trim a healthy miner for efficiency (hold frequency, lower voltage while it still passes):
```bash
python bitaxe_hashrate_benchmark.py 192.168.2.29 --mode efficiency --max-error 3.5
```

Full sweep example:
```bash
python bitaxe_hashrate_benchmark.py 192.168.2.29 -v 1150 -f 500
```

Rescue one unstable ASIC (hold frequency, raise voltage until the error clears the ceiling):
```bash
python bitaxe_hashrate_benchmark.py 192.168.2.29 --mode refine --max-error 3.5 --max-temp 68
```

### Docker Usage (Optional)

Run the container with your Bitaxe's IP address:

```bash
docker run --rm bitaxe-benchmark <bitaxe_ip> [options]
```

Example:
```bash
docker run --rm bitaxe-benchmark 192.168.2.26 -v 1200 -f 550
```

## Benchmarking process

Every combination runs the same measured cycle:

1. **Apply and verify**: set the voltage/frequency, reboot, wait 90s to stabilize, then read the settings back to confirm the device actually took them (a failed apply or watchdog reboot won't mislabel a result).
2. **Measure**: sample every 15s over the window (default 10 min). Transient post-reboot readings (sensors not ready yet) are skipped rather than aborting the combo.
3. **Reduce**: average hashrate (dropping 3 high + 3 low outliers), temperature, VR temperature and power (excluding the warmup samples), compute J/TH, and derive the error rate as a trimmed mean of `errorPercentage` over the stable window. A combo that clearly can't reach the error ceiling is stopped early but still recorded.
4. **Decide the next combo** (per mode, below).
5. **Select and apply**: once the sweep ends, pick the most efficient (lowest J/TH) setting that stayed under the error ceiling with in-tolerance hashrate; if nothing qualifies, fall back to the lowest-error setting. Apply it, save JSON + CSV, and print the ranked summary.

How step 4 differs by mode:

- **grid**: start conservative (default 1150mV / 500MHz) and climb. If the combo is stable and under the ceiling, raise frequency; if it's unstable, over the ceiling, or thermally capped, step frequency back and raise voltage. Stops at the voltage/frequency limits.
- **refine**: hold the frequency and sweep voltage up to the first setting that clears the ceiling, then probe down for a leaner one. If the chip hits the temperature ceiling before the error clears, lower the frequency and retry.
- **efficiency**: hold the frequency and sweep voltage down from the current setting to the lowest voltage that still clears the ceiling.
- **--check**: measure the current setting once and report; no changes, no reboot.

The miner reboots between combos and mining is interrupted for the whole run. The tool restores the best setting it found (or your original) on exit and on Ctrl+C, and skips the 90s stabilization wait on that final restore.

## Configuration

The script includes several configurable parameters:

- Maximum chip temperature: 66°C (override with `--max-temp`)
- Maximum VR temperature: 86°C
- Maximum allowed voltage: 1400mV
- Minimum allowed voltage: 1000mV
- Maximum allowed frequency: 1200MHz
- Maximum power consumption: 40W
- Minimum allowed frequency: 400MHz
- Minimum input voltage: 4800mV
- Maximum input voltage: 5500mV
- Benchmark duration: 10 minutes (override with `--benchmark-time`)
- Sample interval: 15 seconds
- Sleep time before benchmark: 90 seconds
- Error-rate ceiling: 3.5% (override with `--max-error`)
- Minimum required samples: at least 8 post-warmup (so `--benchmark-time` must be at least 210s at the default 15s interval)
- Voltage increment: 20mV (override with `--voltage-step`)
- Frequency increment: 25MHz (override with `--frequency-step`)

## A note on the error metric

The device's `errorPercentage` is a noisy rolling rate, not a cumulative ratio, and it can swing from sample to sample. The per-combo figure is an average of `errorPercentage` across the stable window (warmup samples excluded), which reproduces the number AxeOS reports. With 5 or more samples it uses a light trimmed mean (dropping one extreme at each end) so a single noisy sample won't push a borderline combo over the ceiling, and the spread (`std`/`min`/`max`) is recorded alongside it. The raw `errorCount` delta over the fixed-length window is recorded separately as `errorCountDelta`, a comparable measure of error volume. If a device does not expose error data, the error rate is reported as unknown, a one-time notice is printed, and it never disqualifies a combination.

## Output

Results are saved to `bitaxe_benchmark_results_<ip_address>_<timestamp>.json` and `.csv`, containing:
- Complete test results for all combinations
- Top 5 by hashrate, top 5 by efficiency (J/TH), and top 5 by lowest error rate
- For each configuration:
  - Average hashrate (with outlier removal)
  - Temperature readings (excluding initial warmup period)
  - VR temperature readings (when available)
  - Power efficiency metrics (J/TH)
  - Error rate (`errorRate`) with its dispersion (`errorRateStd`) and raw hardware errors over the window (`errorCountDelta`)
  - Whether it stayed within the error ceiling (`passedErrorGate`) and held hashrate in tolerance (`hashrateWithinTolerance`)
  - Whether the window was stopped early as clearly-failing (`earlyAborted`)
  - Voltage/frequency combinations tested

## Safety Features

- Automatic temperature monitoring with configurable safety cutoff (66°C chip temp default)
- Voltage regulator (VR) temperature monitoring with safety cutoff (86°C)
- Input voltage monitoring with minimum threshold (4800mV) and maximum threshold (5500mV)
- Power consumption monitoring with safety cutoff (40W)
- Transient warmup readings (temp/voltage/hashrate not ready after a reboot) are skipped, not treated as failures
- Graceful shutdown on interruption (Ctrl+C)
- Automatic reset to best performing settings after benchmarking
- Input validation for safe voltage and frequency ranges
- Hashrate validation to ensure stability
- Protection against invalid system data
- Outlier removal from benchmark results

## Testing

The decision logic (error math, gate, winner selection, return-value shape) is covered by unit tests that need no hardware:

```bash
python -m pytest tests/
```

## Data Processing

The tool implements several data processing techniques to ensure accurate results:
- Removes 3 highest and 3 lowest hashrate readings to eliminate outliers (when enough samples exist)
- Excludes warmup readings (chronologically) when averaging temperature, power, and error rate
- Validates hashrate is within 6% of theoretical maximum
- Averages power consumption over the post-warmup window (the same window as hashrate/error, so J/TH is comparable across settings)
- Monitors VR temperature when available
- Calculates efficiency in Joules per Terahash (J/TH)

## Credits

Fork of [mrv777/Bitaxe-Hashrate-Benchmark](https://github.com/mrv777/Bitaxe-Hashrate-Benchmark). All upstream functionality is preserved; the error-aware selection, the refine / efficiency / check modes, and the configurable ceilings are additions on top.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

This project is licensed under the GNU General Public License v3.0 - see the [LICENSE](LICENSE) file for details.

## Disclaimer

Please use this tool responsibly. Overclocking and voltage modifications can potentially damage your hardware if not done carefully. Always ensure proper cooling and monitor your device during benchmarking.
