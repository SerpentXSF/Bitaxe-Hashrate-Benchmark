# Bitaxe Hashrate Benchmark (error-aware)

A Python-based benchmarking tool for optimizing Bitaxe mining performance by testing different voltage and frequency combinations while monitoring hashrate, temperature, power efficiency, **and ASIC hardware error rate**.

This is an error-aware fork of [mrv777/Bitaxe-Hashrate-Benchmark](https://github.com/mrv777/Bitaxe-Hashrate-Benchmark). The upstream tool selects the setting with the highest hashrate and reports J/TH separately; it does not consider the ASIC error rate. On BM1370 boards an aggressive undervolt can look great on hashrate and efficiency while the chip throws double-digit hardware errors — which is exactly the setting the original picks. This fork measures the error rate per combination, refuses settings above a configurable ceiling, and selects the most efficient setting that stays under it.

## What this fork adds

- **Error-rate measurement** — the mean `errorPercentage` over each combo's stable window, plus the raw ASIC error count accrued (`errorCountDelta`).
- **Error gate → efficiency selection** — the "best" setting is the lowest J/TH among combinations that stay within the error ceiling (`--max-error`, default 3.5%), instead of the raw fastest. Falls back to the lowest-error setting when nothing clears the ceiling.
- **`refine` mode** — a fast single-ASIC rescue path. Sweeps voltage upward to the lowest setting that clears the error ceiling, then probes *downward* for a leaner (better J/TH) passer. If the chip is thermally boxed in — error still too high when the temperature ceiling is reached — it automatically drops the frequency and retries, since frequency, not voltage, is the lever at that point. Combos that clearly can't clear the ceiling are aborted early (but still recorded as a fallback floor) to save time.
- **`efficiency` mode** — for an already-healthy miner: holds the frequency and trims voltage *down* from the current setting to the leanest voltage that still clears the ceiling, cutting power/heat with no loss of hashrate.
- **Robustness** — settings are read back and confirmed after each apply (a failed PATCH or watchdog reboot can't mislabel a result); a transient network blip retries instead of ending the run; error dispersion (std/min/max) is recorded and the gate uses a light trimmed mean so a lone spike can't tip a borderline combo. The benchmark returns a structured result internally, so adding a field can never shift a positional unpack.
- **Configurable chip-temp ceiling** (`--max-temp`) — BM1370/Gamma boards often idle near 65 °C and need 68 to have room to raise voltage.
- **CSV export and a ranked summary table** (highest hashrate / most efficient / lowest error) alongside the existing JSON.
- **Resume** (`--resume`) — reload prior results for an IP and skip already-tested combinations.
- The benchmark logic is unit-tested (see `tests/`), and the run is wrapped in `main()` so it can be imported without executing.

Everything is a backward-compatible superset: an existing invocation behaves as before, now with the error rate also shown.

## Which mode should I use?

| Your situation | Use | What it does |
|---|---|---|
| Just want a quick health snapshot — **no changes** | `--check` | Measures the current setting once (no reboot) and reports error / J-TH / temp |
| Fresh chip, or you want the full voltage×frequency picture | `--mode grid` (default) | Full sweep; picks the most efficient setting under the error ceiling |
| Miner is **erroring / unstable** and you want it fixed | `--mode refine` | Sweeps voltage up (and drops frequency if it overheats) until the error clears |
| Miner is **already healthy** and you want less power/heat | `--mode efficiency` | Holds the frequency and trims voltage down to the leanest setting that still passes |

`refine` and `efficiency` start from the device's **current** setting; `grid` starts from a conservative default and climbs. Every mode auto-restores the best setting it found (or your original) on exit, including Ctrl+C.

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
- `-v, --voltage`: Initial voltage in mV (grid default: 1150; refine default: device current)
- `-f, --frequency`: Initial frequency in MHz (grid default: 500; refine default: device current)
- `--mode {grid,refine,efficiency}`: full sweep (default), rescue an unstable ASIC, or trim voltage down on a healthy one
- `--max-error <pct>`: error-rate ceiling for selecting the best setting (default: 3.5)
- `--max-temp <C>`: chip temperature cutoff (default: 66; BM1370/Gamma often need 68)
- `--no-error-gate`: report the error rate but do not use it to gate selection
- `--resume`: reload prior results for this IP and skip already-tested combos
- `--benchmark-time <s>`: override the per-combo window in seconds (default: 600)
- `--voltage-step <mV>` / `--frequency-step <MHz>`: sweep increments (defaults: 20 / 25)
- `--dry-run`: print the resolved plan and exit without touching the device
- `--check`: read-only — measure the current setting once (no changes, no reboot) and report. Uses a shorter ~240s window unless `--benchmark-time` is given.

Quick health check (no changes to the miner). On a Gamma that idles near 66–67 °C, pass `--max-temp 68` so the check isn't cut short by the temperature cutoff:
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
- **Minimum required samples: at least 8 post-warmup** (so `--benchmark-time` must be ≥ 210s at the default 15s interval)
- Voltage increment: 20mV (override with `--voltage-step`)
- Frequency increment: 25MHz (override with `--frequency-step`)

## A note on the error metric

The device's `errorPercentage` is a noisy rolling rate, not a cumulative ratio — it can swing sample to sample. The per-combo figure is therefore an average of `errorPercentage` across the stable window (warmup samples excluded), which reproduces the number AxeOS reports. With ≥5 samples it uses a **light trimmed mean** (dropping one extreme at each end) so a lone spike can't tip a borderline combo across the ceiling, and the dispersion (`std`/`min`/`max`) is recorded alongside it. The raw `errorCount` delta over the fixed-length window is recorded separately as `errorCountDelta`, a directly-comparable error-volume diagnostic. If a device does not expose error data, the error rate is reported as unknown, a one-time notice is printed, and it never disqualifies a combination.

## Output

Results are saved to `bitaxe_benchmark_results_<ip_address>_<timestamp>.json` and `.csv`, containing:
- Complete test results for all combinations
- Top 5 by hashrate, top 5 by efficiency (J/TH), and top 5 by lowest error rate
- For each configuration:
  - Average hashrate (with outlier removal)
  - Temperature readings (excluding initial warmup period)
  - VR temperature readings (when available)
  - Power efficiency metrics (J/TH)
  - Error rate (`errorRate`) and raw hardware errors over the window (`errorCountDelta`)
  - Whether it stayed within the error ceiling (`passedErrorGate`)
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

The benchmark's decision logic (error math, gate, winner selection, return-value shape) is covered by unit tests that need no hardware:

```bash
python -m pytest tests/
```

## Data Processing

The tool implements several data processing techniques to ensure accurate results:
- Removes 3 highest and 3 lowest hashrate readings to eliminate outliers (when enough samples exist)
- Excludes warmup readings (chronologically) when averaging temperature, power, and error rate
- Validates hashrate is within 6% of theoretical maximum
- Averages power consumption over the post-warmup window (same window as hashrate/error, so J/TH is comparable across settings)
- Monitors VR temperature when available
- Calculates efficiency in Joules per Terahash (J/TH)

## Credits

Fork of [mrv777/Bitaxe-Hashrate-Benchmark](https://github.com/mrv777/Bitaxe-Hashrate-Benchmark). All upstream functionality is preserved; the error-aware selection, refine mode, and configurable ceilings are additions on top.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

This project is licensed under the GNU General Public License v3.0 - see the [LICENSE](LICENSE) file for details.

## Disclaimer

Please use this tool responsibly. Overclocking and voltage modifications can potentially damage your hardware if not done carefully. Always ensure proper cooling and monitor your device during benchmarking.
