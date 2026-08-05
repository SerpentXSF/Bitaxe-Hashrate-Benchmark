import requests
import time
import json
import csv
import glob
import signal
import sys
import argparse
from datetime import datetime

START_TIME = datetime.now().strftime("%Y-%m-%d_%H")

# ANSI Color Codes
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
RESET = "\033[0m"

# Configuration
voltage_increment = 20
frequency_increment = 25
sleep_time = 90               # Wait 90 seconds before starting the benchmark
benchmark_time = 600          # 10 minutes benchmark time
sample_interval = 15          # 15 seconds sample interval
max_temp = 66                 # Will stop if temperature reaches or exceeds this value
max_allowed_voltage = 1400    # Maximum allowed core voltage
max_allowed_frequency = 1200  # Maximum allowed core frequency
max_vr_temp = 86              # Maximum allowed voltage regulator temperature
min_input_voltage = 4800      # Minimum allowed input voltage
max_input_voltage = 5500      # Maximum allowed input voltage
max_power = 40                # Max of 40W because of DC plug
min_allowed_voltage = 1000    # Minimum allowed core voltage
min_allowed_frequency = 400   # Minimum allowed frequency

# Warmup samples excluded from temperature and error-rate windows
warmup_samples = 6

# Runtime configuration (populated in main from CLI args)
bitaxe_ip = None
initial_voltage = None
initial_frequency = None
max_error_rate = 3.5          # Error-rate ceiling in percent (default)
error_gate_enabled = True     # Discard combos above the ceiling when selecting best
benchmark_mode = "grid"       # "grid" or "refine"
resume_enabled = False

# Determined from the device
small_core_count = None
asic_count = None
default_voltage = None
default_frequency = None

# Results storage
results = []
results_basename_override = None   # set by --resume to keep writing the same file

# Abort reasons that mean "too hot / too much power" — the lever is frequency, not voltage
THERMAL_REASONS = ("CHIP_TEMP_EXCEEDED", "VR_TEMP_EXCEEDED", "POWER_CONSUMPTION_EXCEEDED")

# State flags
handling_interrupt = False
system_reset_done = False


def parse_arguments():
    parser = argparse.ArgumentParser(description='Bitaxe Hashrate Benchmark Tool (error-aware)')
    parser.add_argument('bitaxe_ip', nargs='?', help='IP address of the Bitaxe (e.g., 192.168.2.26)')
    parser.add_argument('-v', '--voltage', type=int, default=None,
                        help='Initial voltage in mV (grid default: 1150; refine default: device current)')
    parser.add_argument('-f', '--frequency', type=int, default=None,
                        help='Initial frequency in MHz (grid default: 500; refine default: device current)')
    parser.add_argument('--mode', choices=['grid', 'refine'], default='grid',
                        help="'grid' full sweep (default) or 'refine' voltage-only sweep at a fixed frequency")
    parser.add_argument('--max-error', type=float, default=3.5,
                        help='Error-rate ceiling in percent for selecting the best setting (default: 3.5)')
    parser.add_argument('--max-temp', type=int, default=66,
                        help='Chip temperature cutoff in C (default: 66; BM1370/Gamma often need 68)')
    parser.add_argument('--no-error-gate', action='store_true',
                        help='Report error rate but do not use it to gate the best setting (legacy pick behavior)')
    parser.add_argument('--resume', action='store_true',
                        help='Reload existing results for this IP and skip already-tested combos')
    parser.add_argument('--benchmark-time', type=int, default=None,
                        help='Override per-combo benchmark window in seconds (default: 600)')

    # If no arguments are provided, print help and exit
    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(1)

    return parser.parse_args()


# --------------------------------------------------------------------------- #
# Pure helpers (no globals, no I/O) — unit-tested in tests/                    #
# --------------------------------------------------------------------------- #

def sum_error_count(info):
    """Sum the raw ASIC error counts from a /api/system/info payload.

    Returns an int, or None if the field is not present (older firmware)."""
    monitor = info.get("hashrateMonitor") if isinstance(info, dict) else None
    if not monitor:
        return None
    asics = monitor.get("asics")
    if not asics:
        return None
    total = 0
    found = False
    for asic in asics:
        ec = asic.get("errorCount")
        if ec is not None:
            total += ec
            found = True
    return total if found else None


def compute_window_error(samples):
    """Mean ASIC error rate (percent) over a combo's post-warmup samples.

    The device's `errorPercentage` is a noisy rolling rate (not a cumulative
    ratio), so averaging it across the stable window is the faithful, robust
    metric — it matches what AxeOS reports. Each sample is a dict with
    'error_percentage' (float|None). Returns (rate_or_None, method_string)."""
    eps = [s['error_percentage'] for s in samples if s.get('error_percentage') is not None]
    if eps:
        return (sum(eps) / len(eps), 'errorPercentage-mean')
    return (None, 'unavailable')


def window_error_count(samples):
    """Raw ASIC hardware errors accrued across the window.

    Because every combo runs the same fixed-length window, this is a directly
    comparable error-volume diagnostic. Summing consecutive positive deltas
    (rather than just endpoints) stays correct even if the counter resets on a
    mid-window reboot. Returns an int, or None if the device does not expose a
    raw error count."""
    counts = [s['error_count'] for s in samples if s.get('error_count') is not None]
    if len(counts) < 2:
        return None
    total = 0
    for prev, cur in zip(counts, counts[1:]):
        if cur >= prev:
            total += cur - prev
        # a decrease means the counter reset; skip that step rather than count it
    return total


def passes_error_gate(error_rate, ceiling):
    """A combo passes when its error rate is unknown (None) or within the ceiling."""
    return error_rate is None or error_rate <= ceiling


def select_best(all_results, ceiling, gate_enabled=True):
    """Pick the best result: lowest J/TH among gate passers, ties to higher hashrate.

    A passer must both stay within the error ceiling and hold its hashrate in
    tolerance (a throttling combo with depressed hashrate must not win). Falls
    back to the lowest-error, in-tolerance combo when nothing passes the gate.
    Returns the chosen result dict, or None when there are no results."""
    if not all_results:
        return None

    def in_tolerance(r):
        return r.get('hashrateWithinTolerance', True)

    if gate_enabled:
        passers = [r for r in all_results if r.get('passedErrorGate') and in_tolerance(r)]
    else:
        passers = [r for r in all_results if in_tolerance(r)]

    if passers:
        return sorted(passers, key=lambda r: (r['efficiencyJTH'], -r['averageHashRate']))[0]

    # Nothing passed — prefer in-tolerance, then lowest error, then efficiency.
    def err_key(r):
        er = r.get('errorRate')
        return er if er is not None else float('inf')

    return sorted(all_results, key=lambda r: (not in_tolerance(r), err_key(r), r['efficiencyJTH']))[0]


# --------------------------------------------------------------------------- #
# Device I/O                                                                   #
# --------------------------------------------------------------------------- #

def fetch_default_settings():
    """Read the device's CURRENT voltage/frequency (the refine start point and the
    restore-on-exit baseline) plus core/ASIC counts. Current settings come from
    /api/system/info; only genuinely missing pieces are backfilled from
    /api/system/asic (whose defaultVoltage/defaultFrequency are STOCK values, not
    the running ones — so they must never overwrite present current settings)."""
    global default_voltage, default_frequency, small_core_count, asic_count

    try:
        response = requests.get(f"{bitaxe_ip}/api/system/info", timeout=10)
        response.raise_for_status()
        system_info = response.json()
    except requests.exceptions.RequestException as e:
        print(RED + f"Error fetching from /api/system/info: {e}" + RESET)
        sys.exit(1)

    if "smallCoreCount" not in system_info:
        print(RED + "Error: smallCoreCount field missing from /api/system/info response." + RESET)
        print(RED + "Cannot proceed without core count information for hashrate calculations." + RESET)
        sys.exit(1)

    small_core_count = system_info.get("smallCoreCount")
    default_voltage = system_info.get("coreVoltage")
    default_frequency = system_info.get("frequency")
    asic_count = system_info.get("asicCount")

    # Backfill only what /info didn't provide from the newer /api/system/asic split.
    if default_voltage is None or default_frequency is None or asic_count is None:
        try:
            asic_info = requests.get(f"{bitaxe_ip}/api/system/asic", timeout=10).json()
            if default_voltage is None:
                default_voltage = asic_info.get("defaultVoltage")
            if default_frequency is None:
                default_frequency = asic_info.get("defaultFrequency")
            if asic_count is None:
                asic_count = asic_info.get("asicCount")
            print(YELLOW + "Backfilled missing fields from /api/system/asic." + RESET)
        except requests.exceptions.RequestException as e:
            print(YELLOW + f"Could not reach /api/system/asic: {e}" + RESET)

    # Final safety fallbacks.
    if default_voltage is None:
        default_voltage = 1150
    if default_frequency is None:
        default_frequency = 500
    if asic_count is None:
        asic_count = 1

    print(GREEN + f"Current settings determined:\n"
                  f"  Core Voltage: {default_voltage}mV\n"
                  f"  Frequency: {default_frequency}MHz\n"
                  f"  ASIC Configuration: {small_core_count * asic_count} total cores" + RESET)


def handle_sigint(signum, frame):
    global system_reset_done, handling_interrupt

    if handling_interrupt or system_reset_done:
        return

    handling_interrupt = True
    print(RED + "Benchmarking interrupted by user." + RESET)

    try:
        if results:
            reset_to_best_setting()
            save_results()
            print(GREEN + "Bitaxe reset to best or default settings and results saved." + RESET)
        else:
            print(YELLOW + "No valid benchmarking results found. Applying predefined default settings." + RESET)
            set_system_settings(default_voltage, default_frequency)
    finally:
        system_reset_done = True
        handling_interrupt = False
        sys.exit(0)


def get_system_info():
    retries = 5
    for attempt in range(retries):
        try:
            response = requests.get(f"{bitaxe_ip}/api/system/info", timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.Timeout:
            print(YELLOW + f"Timeout while fetching system info. Attempt {attempt + 1} of {retries}." + RESET)
        except requests.exceptions.ConnectionError:
            print(RED + f"Connection error while fetching system info. Attempt {attempt + 1} of {retries}." + RESET)
        except requests.exceptions.RequestException as e:
            print(RED + f"Error fetching system info: {e}" + RESET)
            break
        time.sleep(5)
    return None


def set_system_settings(core_voltage, frequency):
    settings = {
        "coreVoltage": core_voltage,
        "frequency": frequency
    }
    try:
        response = requests.patch(f"{bitaxe_ip}/api/system", json=settings, timeout=10)
        response.raise_for_status()
        print(YELLOW + f"Applying settings: Voltage = {core_voltage}mV, Frequency = {frequency}MHz" + RESET)
        time.sleep(2)
        restart_system()
    except requests.exceptions.RequestException as e:
        print(RED + f"Error setting system settings: {e}" + RESET)


def restart_system():
    try:
        is_interrupt = handling_interrupt

        # Restart here as some bitaxes get unstable with bad settings.
        if not is_interrupt:
            print(YELLOW + f"Applying new settings and waiting {sleep_time}s for system stabilization..." + RESET)
            response = requests.post(f"{bitaxe_ip}/api/system/restart", timeout=10)
            response.raise_for_status()
            time.sleep(sleep_time)
        else:
            print(YELLOW + "Applying final settings..." + RESET)
            response = requests.post(f"{bitaxe_ip}/api/system/restart", timeout=10)
            response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(RED + f"Error restarting the system: {e}" + RESET)


def apply_settings(core_voltage, frequency):
    """Apply settings, restart, and confirm the device actually took them.

    A silently-failed PATCH or a watchdog reboot into different settings would
    otherwise make benchmark_iteration measure the wrong configuration and
    record it under the requested one. Retries once, then gives up. Returns
    True only when the device reports back the requested voltage/frequency."""
    for attempt in range(2):
        set_system_settings(core_voltage, frequency)
        info = get_system_info()
        if info and info.get("coreVoltage") == core_voltage and info.get("frequency") == frequency:
            return True
        got_v = info.get("coreVoltage") if info else "?"
        got_f = info.get("frequency") if info else "?"
        print(YELLOW + f"Applied settings not confirmed (attempt {attempt + 1}/2): requested "
                       f"{core_voltage}mV/{frequency}MHz, device reports {got_v}mV/{got_f}MHz." + RESET)
    return False


def _iter_fail(reason):
    """Uniform failure return for benchmark_iteration, matching the 8-value
    success tuple: (hashrate, temp, jth, hashrate_ok, vr_temp, reason, error_rate,
    error_count_delta)."""
    return None, None, None, False, None, reason, None, None


def benchmark_iteration(core_voltage, frequency):
    current_time = time.strftime("%H:%M:%S")
    print(GREEN + f"[{current_time}] Starting benchmark for Core Voltage: {core_voltage}mV, Frequency: {frequency}MHz" + RESET)
    hash_rates = []
    temperatures = []
    power_consumptions = []
    vr_temps = []
    error_samples = []
    total_samples = benchmark_time // sample_interval
    expected_hashrate = frequency * ((small_core_count * asic_count) / 1000)

    for sample in range(total_samples):
        info = get_system_info()
        if info is None:
            print(YELLOW + "Skipping this iteration due to failure in fetching system info." + RESET)
            return _iter_fail("SYSTEM_INFO_FAILURE")

        temp = info.get("temp")
        vr_temp = info.get("vrTemp")
        voltage = info.get("voltage")

        # Right after a reboot the sensors can briefly read null/near-zero. Skip
        # those samples rather than aborting the whole combo over a warmup glitch.
        if temp is None or temp < 5 or voltage is None:
            print(YELLOW + "Sensor warming up (temp/voltage not ready yet); skipping sample." + RESET)
            if sample < total_samples - 1:
                time.sleep(sample_interval)
            continue

        if temp >= max_temp:
            print(RED + f"Chip temperature exceeded {max_temp}°C! Stopping current benchmark." + RESET)
            return _iter_fail("CHIP_TEMP_EXCEEDED")

        if vr_temp is not None and vr_temp >= max_vr_temp:
            print(RED + f"Voltage regulator temperature exceeded {max_vr_temp}°C! Stopping current benchmark." + RESET)
            return _iter_fail("VR_TEMP_EXCEEDED")

        if voltage < min_input_voltage:
            print(RED + f"Input voltage is below the minimum allowed value of {min_input_voltage}mV! Stopping current benchmark." + RESET)
            return _iter_fail("INPUT_VOLTAGE_BELOW_MIN")

        if voltage > max_input_voltage:
            print(RED + f"Input voltage is above the maximum allowed value of {max_input_voltage}mV! Stopping current benchmark." + RESET)
            return _iter_fail("INPUT_VOLTAGE_ABOVE_MAX")

        hash_rate = info.get("hashRate")
        power_consumption = info.get("power")

        if hash_rate is None or power_consumption is None:
            print(YELLOW + "Hashrate or Watts data not ready yet; skipping sample." + RESET)
            if sample < total_samples - 1:
                time.sleep(sample_interval)
            continue

        if power_consumption > max_power:
            print(RED + f"Power consumption exceeded {max_power}W! Stopping current benchmark." + RESET)
            return _iter_fail("POWER_CONSUMPTION_EXCEEDED")

        hash_rates.append(hash_rate)
        temperatures.append(temp)
        power_consumptions.append(power_consumption)
        if vr_temp is not None and vr_temp > 0:
            vr_temps.append(vr_temp)
        error_samples.append({
            "error_count": sum_error_count(info),
            "error_percentage": info.get("errorPercentage"),
        })

        percentage_progress = ((sample + 1) / total_samples) * 100
        status_line = (
            f"[{sample + 1:2d}/{total_samples:2d}] "
            f"{percentage_progress:5.1f}% | "
            f"CV: {core_voltage:4d}mV | "
            f"F: {frequency:4d}MHz | "
            f"H: {int(hash_rate):4d} GH/s | "
            f"IV: {int(voltage):4d}mV | "
            f"T: {int(temp):2d}°C"
        )
        if vr_temp is not None and vr_temp > 0:
            status_line += f" | VR: {int(vr_temp):2d}°C"
        er_now = info.get("errorPercentage")
        if er_now is not None:
            status_line += f" | E: {er_now:4.1f}%"
        print(status_line + RESET)

        # Early abort: if even a perfect (0%) remainder can't pull the window mean
        # under the ceiling, this combo is hopeless — don't waste the rest of it.
        if error_gate_enabled:
            post = error_samples[warmup_samples:]
            eps_post = [s['error_percentage'] for s in post if s['error_percentage'] is not None]
            total_post = total_samples - warmup_samples
            if len(eps_post) >= 4 and total_post > 0:
                best_case_mean = sum(eps_post) / total_post
                if best_case_mean > max_error_rate:
                    print(RED + f"Error ceiling unreachable (best-case mean {best_case_mean:.1f}% > "
                                f"{max_error_rate:.1f}%); aborting combo early." + RESET)
                    return _iter_fail("ERROR_CEILING_UNREACHABLE")

        if sample < total_samples - 1:
            time.sleep(sample_interval)

    if hash_rates and temperatures and power_consumptions:
        # Remove 3 highest and 3 lowest hashrates in case of outliers (only when
        # there are enough samples for trimming to leave data behind).
        sorted_hashrates = sorted(hash_rates)
        trimmed_hashrates = sorted_hashrates[3:-3] if len(sorted_hashrates) > 6 else sorted_hashrates
        average_hashrate = sum(trimmed_hashrates) / len(trimmed_hashrates)

        # Sort and trim temperatures (remove lowest readings during warmup)
        sorted_temps = sorted(temperatures)
        trimmed_temps = sorted_temps[warmup_samples:] if len(sorted_temps) > warmup_samples else sorted_temps
        average_temperature = sum(trimmed_temps) / len(trimmed_temps)

        average_vr_temp = None
        if vr_temps:
            sorted_vr_temps = sorted(vr_temps)
            trimmed_vr_temps = sorted_vr_temps[warmup_samples:] if len(sorted_vr_temps) > warmup_samples else sorted_vr_temps
            average_vr_temp = sum(trimmed_vr_temps) / len(trimmed_vr_temps)

        # Average power over the same post-warmup window as hashrate/error so the
        # J/TH comparison across combos is apples-to-apples (warmup power is low
        # and would bias efficiency optimistic).
        trimmed_power = power_consumptions[warmup_samples:] if len(power_consumptions) > warmup_samples else power_consumptions
        average_power = sum(trimmed_power) / len(trimmed_power)

        if average_hashrate > 0:
            efficiency_jth = average_power / (average_hashrate / 1_000)
        else:
            print(RED + "Warning: Zero hashrate detected, skipping efficiency calculation" + RESET)
            return _iter_fail("ZERO_HASHRATE")

        # Error rate from the stable tail of the window (skip warmup samples).
        error_window = error_samples[warmup_samples:]
        error_rate, error_method = compute_window_error(error_window)
        error_count_delta = window_error_count(error_window)

        hashrate_within_tolerance = (average_hashrate >= expected_hashrate * 0.94)

        print(GREEN + f"Average Hashrate: {average_hashrate:.2f} GH/s (Expected: {expected_hashrate:.2f} GH/s)" + RESET)
        print(GREEN + f"Average Temperature: {average_temperature:.2f}°C" + RESET)
        if average_vr_temp is not None:
            print(GREEN + f"Average VR Temperature: {average_vr_temp:.2f}°C" + RESET)
        print(GREEN + f"Efficiency: {efficiency_jth:.2f} J/TH" + RESET)
        if error_rate is not None:
            gate_note = "PASS" if passes_error_gate(error_rate, max_error_rate) else "OVER CEILING"
            colour = GREEN if gate_note == "PASS" else RED
            extra = f", {error_count_delta} hw errors" if error_count_delta is not None else ""
            print(colour + f"Error Rate: {error_rate:.2f}% (mean{extra}) [{gate_note} @ {max_error_rate:.1f}%]" + RESET)
        else:
            print(YELLOW + "Error Rate: unavailable (no error data from device)" + RESET)

        return (average_hashrate, average_temperature, efficiency_jth, hashrate_within_tolerance,
                average_vr_temp, None, error_rate, error_count_delta)
    else:
        print(YELLOW + "No Hashrate or Temperature or Watts data collected." + RESET)
        return None, None, None, False, None, "NO_DATA_COLLECTED", None, None


def record_result(core_voltage, frequency, avg_hashrate, avg_temp, efficiency_jth,
                  avg_vr_temp, error_rate, error_count_delta=None, hashrate_ok=True):
    result = {
        "coreVoltage": core_voltage,
        "frequency": frequency,
        "averageHashRate": avg_hashrate,
        "averageTemperature": avg_temp,
        "efficiencyJTH": efficiency_jth,
        "errorRate": error_rate,
        "errorCountDelta": error_count_delta,
        "passedErrorGate": passes_error_gate(error_rate, max_error_rate),
        "hashrateWithinTolerance": hashrate_ok,
    }
    if avg_vr_temp is not None:
        result["averageVRTemp"] = avg_vr_temp
    results.append(result)
    return result


def already_tested(core_voltage, frequency):
    return any(r["coreVoltage"] == core_voltage and r["frequency"] == frequency for r in results)


def results_filename(ext):
    if results_basename_override:
        return f"{results_basename_override}.{ext}"
    ip_address = bitaxe_ip.replace('http://', '').replace(':', '_')
    return f"bitaxe_benchmark_results_{ip_address}_{START_TIME}.{ext}"


def load_existing_results():
    """For --resume: reload the most recent prior results file for this IP and keep
    writing to it. Globbing the timestamp (rather than assuming the current hour)
    means a resume that crosses an hour boundary still finds and continues the run."""
    global results, results_basename_override
    ip_address = bitaxe_ip.replace('http://', '').replace(':', '_')
    matches = sorted(glob.glob(f"bitaxe_benchmark_results_{ip_address}_*.json"))
    if not matches:
        print(YELLOW + "Resume: no prior results file found for this IP; starting fresh." + RESET)
        return
    latest = matches[-1]  # %Y-%m-%d_%H timestamps sort chronologically
    try:
        with open(latest, "r") as f:
            data = json.load(f)
    except (IOError, ValueError):
        print(YELLOW + f"Resume: could not read {latest}; starting fresh." + RESET)
        return
    prior = data.get("all_results", data) if isinstance(data, dict) else data
    if isinstance(prior, list):
        results = prior
        results_basename_override = latest[:-5]  # strip '.json' — keep writing this file
        print(GREEN + f"Resume: loaded {len(results)} prior result(s) from {latest}" + RESET)


def save_results():
    try:
        filename = results_filename("json")
        with open(filename, "w") as f:
            json.dump(results, f, indent=4)
        print(GREEN + f"Results saved to {filename}" + RESET)
        print()
    except IOError as e:
        print(RED + f"Error saving results to file: {e}" + RESET)


def save_csv():
    if not results:
        return
    try:
        filename = results_filename("csv")
        fields = ["coreVoltage", "frequency", "averageHashRate", "efficiencyJTH",
                  "errorRate", "errorCountDelta", "passedErrorGate",
                  "hashrateWithinTolerance", "averageTemperature", "averageVRTemp"]
        with open(filename, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for r in results:
                writer.writerow({k: r.get(k, "") for k in fields})
        print(GREEN + f"CSV saved to {filename}" + RESET)
    except IOError as e:
        print(RED + f"Error saving CSV to file: {e}" + RESET)


def reset_to_best_setting():
    best_result = select_best(results, max_error_rate, error_gate_enabled)
    if best_result is None:
        print(YELLOW + "No valid benchmarking results found. Applying predefined default settings." + RESET)
        set_system_settings(default_voltage, default_frequency)
    else:
        best_voltage = best_result["coreVoltage"]
        best_frequency = best_result["frequency"]
        er = best_result.get("errorRate")
        er_str = f"{er:.2f}%" if er is not None else "n/a"
        if error_gate_enabled and not best_result.get("passedErrorGate"):
            print(YELLOW + "Warning: no setting stayed within the error ceiling; "
                           "applying the lowest-error result found." + RESET)
        print(GREEN + f"Applying the best settings from benchmarking:\n"
                      f"  Core Voltage: {best_voltage}mV\n"
                      f"  Frequency: {best_frequency}MHz\n"
                      f"  Efficiency: {best_result['efficiencyJTH']:.2f} J/TH | Error: {er_str}" + RESET)
        set_system_settings(best_voltage, best_frequency)
    # set_system_settings already restarts; no extra reboot needed here.


def _fmt_row(r):
    er = r.get("errorRate")
    er_str = f"{er:5.2f}%" if er is not None else "  n/a "
    gate = "ok " if r.get("passedErrorGate") else "OVER"
    vr = r.get("averageVRTemp")
    vr_str = f"{vr:4.1f}" if vr is not None else "  - "
    return (f"  {r['coreVoltage']:4d}mV  {r['frequency']:4d}MHz  "
            f"{r['averageHashRate']:7.1f}GH  {r['efficiencyJTH']:5.2f}J/TH  "
            f"err {er_str} [{gate}]  T {r['averageTemperature']:4.1f}  VR {vr_str}")


def print_summary():
    if not results:
        print(RED + "No valid results were found during benchmarking." + RESET)
        return

    top_hash = sorted(results, key=lambda x: x["averageHashRate"], reverse=True)[:5]
    top_eff = sorted(results, key=lambda x: x["efficiencyJTH"])[:5]
    top_lowerr = sorted(results, key=lambda x: (x["errorRate"] if x.get("errorRate") is not None else float('inf')))[:5]

    print(GREEN + "\nTop 5 Highest Hashrate:" + RESET)
    for r in top_hash:
        print(_fmt_row(r))
    print(GREEN + "\nTop 5 Most Efficient (J/TH):" + RESET)
    for r in top_eff:
        print(_fmt_row(r))
    print(GREEN + "\nTop 5 Lowest Error Rate:" + RESET)
    for r in top_lowerr:
        print(_fmt_row(r))

    best = select_best(results, max_error_rate, error_gate_enabled)
    if best is not None:
        er = best.get("errorRate")
        er_str = f"{er:.2f}%" if er is not None else "n/a"
        print(GREEN + f"\nSelected best (error-gate {max_error_rate:.1f}% -> efficiency):" + RESET)
        print(GREEN + f"  {best['coreVoltage']}mV / {best['frequency']}MHz | "
                      f"{best['averageHashRate']:.1f} GH/s | {best['efficiencyJTH']:.2f} J/TH | error {er_str}" + RESET)


def save_final_json():
    """Preserve the upstream all_results / top_performers / most_efficient JSON shape."""
    def slim(r, rank):
        out = {
            "rank": rank,
            "coreVoltage": r["coreVoltage"],
            "frequency": r["frequency"],
            "averageHashRate": r["averageHashRate"],
            "averageTemperature": r["averageTemperature"],
            "efficiencyJTH": r["efficiencyJTH"],
            "errorRate": r.get("errorRate"),
            "errorCountDelta": r.get("errorCountDelta"),
            "passedErrorGate": r.get("passedErrorGate"),
            "hashrateWithinTolerance": r.get("hashrateWithinTolerance"),
        }
        if "averageVRTemp" in r:
            out["averageVRTemp"] = r["averageVRTemp"]
        return out

    top_hash = sorted(results, key=lambda x: x["averageHashRate"], reverse=True)[:5]
    top_eff = sorted(results, key=lambda x: x["efficiencyJTH"])[:5]
    top_lowerr = sorted(results, key=lambda x: (x["errorRate"] if x.get("errorRate") is not None else float('inf')))[:5]

    final_data = {
        "all_results": results,
        "top_performers": [slim(r, i) for i, r in enumerate(top_hash, 1)],
        "most_efficient": [slim(r, i) for i, r in enumerate(top_eff, 1)],
        "lowest_error": [slim(r, i) for i, r in enumerate(top_lowerr, 1)],
    }
    try:
        with open(results_filename("json"), "w") as f:
            json.dump(final_data, f, indent=4)
    except IOError as e:
        print(RED + f"Error saving final JSON: {e}" + RESET)


# --------------------------------------------------------------------------- #
# Benchmark modes                                                             #
# --------------------------------------------------------------------------- #

def run_combo(voltage, frequency):
    """Apply + verify settings, run one benchmark window, and record the result.

    Retries once on a transient info-fetch failure (network blip) so a momentary
    hiccup doesn't end a multi-hour sweep. Returns (recorded_result_or_None,
    error_reason). Records the result (with hashrate tolerance) on success."""
    for attempt in range(2):
        if not apply_settings(voltage, frequency):
            return None, "APPLY_FAILED"
        (avg_hashrate, avg_temp, efficiency_jth, hashrate_ok, avg_vr_temp,
         error_reason, error_rate, error_count_delta) = benchmark_iteration(voltage, frequency)
        if avg_hashrate is not None and avg_temp is not None and efficiency_jth is not None:
            res = record_result(voltage, frequency, avg_hashrate, avg_temp, efficiency_jth,
                                avg_vr_temp, error_rate, error_count_delta, hashrate_ok)
            save_results()
            return res, None
        if error_reason == "SYSTEM_INFO_FAILURE" and attempt == 0:
            print(YELLOW + "Network hiccup during combo; retrying once." + RESET)
            continue
        return None, error_reason
    return None, "SYSTEM_INFO_FAILURE"


def combo_passes(res):
    gate_ok = (not error_gate_enabled) or res.get("passedErrorGate")
    return gate_ok and res.get("hashrateWithinTolerance", True)


def run_grid():
    """Full voltage/frequency sweep, error-aware."""
    current_voltage = initial_voltage
    current_frequency = initial_frequency

    while current_voltage <= max_allowed_voltage and current_frequency <= max_allowed_frequency:
        if resume_enabled and already_tested(current_voltage, current_frequency):
            print(YELLOW + f"Resume: skipping already-tested {current_voltage}mV / {current_frequency}MHz" + RESET)
            current_frequency += frequency_increment
            continue

        res, reason = run_combo(current_voltage, current_frequency)

        if res is not None:
            if combo_passes(res):
                # Stable and within the error ceiling: try a higher frequency.
                if current_frequency + frequency_increment <= max_allowed_frequency:
                    current_frequency += frequency_increment
                else:
                    break
            else:
                # Unstable or over the error ceiling: step frequency back, add voltage.
                if current_voltage + voltage_increment <= max_allowed_voltage:
                    current_voltage += voltage_increment
                    current_frequency -= frequency_increment
                    why = "hashrate" if not res.get("hashrateWithinTolerance", True) else "error rate"
                    print(YELLOW + f"{why} out of range. Decreasing frequency to {current_frequency}MHz "
                                   f"and increasing voltage to {current_voltage}mV" + RESET)
                else:
                    break
        else:
            print(GREEN + "Reached thermal or stability limits. Stopping further testing." + RESET)
            break


def _refine_probe_down(frequency, start_voltage):
    """The starting voltage already passed; probe lower voltages for a leaner
    (better J/TH) setting that still clears the ceiling. select_best picks the
    winner from all recorded passers, so we just need to test them."""
    voltage = start_voltage - voltage_increment
    while voltage >= min_allowed_voltage:
        if resume_enabled and already_tested(voltage, frequency):
            voltage -= voltage_increment
            continue
        res, reason = run_combo(voltage, frequency)
        if res is None:
            return  # thermal/limit/blip while probing down — stop
        if combo_passes(res):
            print(GREEN + f"{voltage}mV also clears the ceiling — trying lower for efficiency." + RESET)
            voltage -= voltage_increment
        else:
            print(YELLOW + f"{voltage}mV no longer clears the ceiling; "
                           f"lowest passer is {voltage + voltage_increment}mV." + RESET)
            return


def _refine_sweep_at(frequency):
    """Sweep voltage upward at one frequency. Returns:
      'passed'    — found an in-tolerance setting under the error ceiling
      'capped'    — hit a thermal/power limit (caller should drop frequency)
      'exhausted' — ran out of voltage headroom or couldn't proceed"""
    voltage = initial_voltage
    first = True
    while min_allowed_voltage <= voltage <= max_allowed_voltage:
        if resume_enabled and already_tested(voltage, frequency):
            print(YELLOW + f"Resume: skipping already-tested {voltage}mV / {frequency}MHz" + RESET)
            voltage += voltage_increment
            continue

        res, reason = run_combo(voltage, frequency)

        if res is not None:
            if combo_passes(res):
                print(GREEN + f"Found stable low-error setting at {voltage}mV / {frequency}MHz." + RESET)
                if first:
                    _refine_probe_down(frequency, voltage)
                return "passed"
            # Recorded but over ceiling / low hashrate: needs more voltage.
            voltage += voltage_increment
            first = False
            print(YELLOW + f"Raising voltage to {voltage}mV to reduce error / stabilize." + RESET)
        elif reason in THERMAL_REASONS:
            return "capped"
        elif reason == "ERROR_CEILING_UNREACHABLE":
            # Too much error at this voltage (aborted early): add voltage and continue.
            voltage += voltage_increment
            first = False
            print(YELLOW + f"Raising voltage to {voltage}mV to reduce error." + RESET)
        else:
            return "exhausted"

    return "exhausted"


def run_refine():
    """Rescue an unstable ASIC: hold a frequency, sweep voltage up to the lowest
    setting that clears the error ceiling (then probe down for efficiency). If the
    chip is thermally boxed in — error still too high when the temp ceiling is hit —
    drop the frequency and try again, since frequency, not voltage, is then the lever."""
    frequency = initial_frequency
    print(GREEN + f"Refine mode: starting at {frequency}MHz, sweeping voltage from "
                  f"{initial_voltage}mV (ceiling {max_error_rate:.1f}% error, {max_temp}°C)." + RESET)

    while frequency >= min_allowed_frequency:
        outcome = _refine_sweep_at(frequency)
        if outcome == "passed":
            return
        if outcome == "capped":
            frequency -= frequency_increment
            if frequency >= min_allowed_frequency:
                print(YELLOW + f"Thermally capped at this frequency; dropping to {frequency}MHz "
                               f"(frequency is the lever once the temp ceiling is hit)." + RESET)
            continue
        # exhausted — no more voltage headroom, or could not proceed
        print(GREEN + "Reached stability limits without clearing the ceiling. Stopping." + RESET)
        return


# --------------------------------------------------------------------------- #
# Entry point                                                                 #
# --------------------------------------------------------------------------- #

def main():
    global bitaxe_ip, initial_voltage, initial_frequency, benchmark_time
    global max_error_rate, error_gate_enabled, benchmark_mode, resume_enabled
    global max_temp, system_reset_done

    args = parse_arguments()
    if not args.bitaxe_ip:
        print(RED + "Error: Bitaxe IP address is required." + RESET)
        sys.exit(1)

    bitaxe_ip = f"http://{args.bitaxe_ip}"
    max_error_rate = args.max_error
    max_temp = args.max_temp
    error_gate_enabled = not args.no_error_gate
    benchmark_mode = args.mode
    resume_enabled = args.resume
    if args.benchmark_time is not None:
        benchmark_time = args.benchmark_time

    total_samples = benchmark_time // sample_interval
    if total_samples - warmup_samples < 8:
        min_time = (warmup_samples + 8) * sample_interval
        raise ValueError(RED + f"Error: Benchmark time too short — only {max(0, total_samples - warmup_samples)} "
                               f"post-warmup samples (need >= 8 for a stable error mean). "
                               f"Use --benchmark-time >= {min_time}." + RESET)

    signal.signal(signal.SIGINT, handle_sigint)

    fetch_default_settings()

    # Resolve start voltage/frequency. Grid keeps the upstream 1150/500 start;
    # refine starts from the device's current settings unless overridden.
    if benchmark_mode == "refine":
        initial_frequency = args.frequency if args.frequency is not None else default_frequency
        initial_voltage = args.voltage if args.voltage is not None else default_voltage
    else:
        initial_frequency = args.frequency if args.frequency is not None else 500
        initial_voltage = args.voltage if args.voltage is not None else 1150

    if not (min_allowed_voltage <= initial_voltage <= max_allowed_voltage):
        raise ValueError(RED + f"Error: Initial voltage {initial_voltage}mV outside allowed range "
                               f"{min_allowed_voltage}-{max_allowed_voltage}mV." + RESET)
    if not (min_allowed_frequency <= initial_frequency <= max_allowed_frequency):
        raise ValueError(RED + f"Error: Initial frequency {initial_frequency}MHz outside allowed range "
                               f"{min_allowed_frequency}-{max_allowed_frequency}MHz." + RESET)

    if resume_enabled:
        load_existing_results()

    print(RED + "\nDISCLAIMER:" + RESET)
    print("This tool will stress test your Bitaxe by running it at various voltages and frequencies.")
    print("While safeguards are in place, running hardware outside of standard parameters carries inherent risks.")
    print("Use this tool at your own risk. The author(s) are not responsible for any damage to your hardware.")
    print("\nNOTE: Ambient temperature significantly affects these results. The optimal settings found may not")
    print("work well if room temperature changes substantially. Re-run the benchmark if conditions change.\n")

    try:
        if benchmark_mode == "refine":
            run_refine()
        else:
            run_grid()
    except Exception as e:
        # Let the finally block own restoration so the device is only reset once.
        print(RED + f"An unexpected error occurred: {e}" + RESET)
    finally:
        if not system_reset_done:
            if results:
                reset_to_best_setting()
                save_results()
                print(GREEN + "Bitaxe reset to best or default settings and results saved." + RESET)
            else:
                print(YELLOW + "No valid benchmarking results found. Applying predefined default settings." + RESET)
                set_system_settings(default_voltage, default_frequency)
            system_reset_done = True

        if results:
            save_csv()
            save_final_json()
            print(GREEN + "\nBenchmarking completed." + RESET)
            print_summary()


if __name__ == "__main__":
    main()
