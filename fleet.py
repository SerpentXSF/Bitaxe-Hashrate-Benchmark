#!/usr/bin/env python3
"""Fleet helper for the Bitaxe benchmark.

Two things:

1. Status (default): read /api/system/info from each miner and print a compact
   table (hashrate, error, temp, VR, fan, J/TH, uptime). Fast and read-only.
   Rows breaching a threshold (error over ceiling, hot chip, pegged fan) are
   flagged so a big table reads as an action list.

2. Run (`--run "<args>"`): run bitaxe_hashrate_benchmark.py on each miner in
   turn with the given args (streaming its output), then print a before/after
   delta and the resulting state of every miner. Exits non-zero if any miner's
   run failed, so it can be scripted.

A `--file` may carry per-miner args after the IP (heterogeneous fleets):
    192.168.2.10 --max-temp 68
    192.168.2.11            # comments allowed
Those are appended to whatever follows `--run`.

Examples:
  python fleet.py 192.168.2.10 192.168.2.11
  python fleet.py --file miners.txt --log fleet_trend.csv
  python fleet.py --file miners.txt --run "--mode refine --max-temp 68 --benchmark-time 300"
"""
import argparse
import csv
import os
import subprocess
import sys

import requests

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bitaxe_hashrate_benchmark.py")

# Thresholds a status row is flagged against (informational only).
ERR_LIMIT = 3.5
TEMP_LIMIT = 66
FAN_LIMIT = 95


def get_info(ip, timeout=6):
    """GET /api/system/info for a miner; None if it can't be reached."""
    try:
        r = requests.get(f"http://{ip}/api/system/info", timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.RequestException:
        return None


def efficiency_jth(info):
    """J/TH from a system-info payload, or None if hashrate is missing/zero."""
    hr = info.get("hashRate") or 0
    power = info.get("power") or 0
    return power / (hr / 1000) if hr else None


def _num(value, fmt, default="n/a"):
    return format(value, fmt) if value is not None else default


def row_flags(info):
    """Actionable flags for a status payload. A pegged fan on its own is normal
    (many Gammas run 100% fixed), so only flag 'cooling' when the fan is maxed
    AND the chip is hot - that is the cooling-limited signal worth acting on."""
    flags = []
    err = info.get("errorPercentage")
    temp = info.get("temp")
    fan = info.get("fanspeed")
    hot = temp is not None and temp >= TEMP_LIMIT
    if err is not None and err > ERR_LIMIT:
        flags.append("err")
    if hot:
        flags.append("temp")
    if hot and fan is not None and fan >= FAN_LIMIT:
        flags.append("cooling")
    return flags


HEADER = ("  {:15} | {:12} | {:>9} | {:>6} | {:>6} | {:>5} | {:>4} | {:>10} | {:>6} | {:>5}"
          .format("ip", "host", "F/CV", "hash", "err", "temp", "vr", "fan", "J/TH", "up"))


def status_row(ip, info):
    if info is None:
        return f"  {ip:15} | UNREACHABLE"
    fan_pct = info.get("fanspeed")
    fan_rpm = info.get("fanrpm")
    fan = f"{fan_pct:.0f}%/{fan_rpm:.0f}" if (fan_pct is not None and fan_rpm is not None) else "n/a"
    jth = efficiency_jth(info)
    up = info.get("uptimeSeconds")
    flags = row_flags(info)
    flag = ("  [!" + " !".join(flags) + "]") if flags else ""
    return ("  {ip:15} | {host:12.12} | {f:>4}/{cv:<4} | {hr:>5}G | {err:>5} | {t:>4} | {vr:>4} | "
            "{fan:>10} | {jth:>6} | {up:>4}m{flag}").format(
        ip=ip,
        host=info.get("hostname", "") or "",
        f=_num(info.get("frequency"), "d", "?"),
        cv=_num(info.get("coreVoltage"), "d", "?"),
        hr=_num(info.get("hashRate"), ".0f"),
        err=_num(info.get("errorPercentage"), ".1f"),
        t=_num(info.get("temp"), ".1f"),
        vr=_num(info.get("vrTemp"), ".0f"),
        fan=fan,
        jth=_num(jth, ".2f"),
        up=_num(up / 60 if up is not None else None, ".0f"),
        flag=flag,
    )


def print_status(ips, title="Fleet status"):
    print(f"\n{title}:")
    print(HEADER)
    for ip in ips:
        print(status_row(ip, get_info(ip)))


def log_status(ips, log_file, timestamp):
    """Append a status snapshot per miner to a CSV for long-term trend tracking."""
    fields = ["timestamp", "ip", "hostname", "frequency", "coreVoltage", "hashRate",
              "errorPercentage", "temp", "vrTemp", "fanspeed", "fanrpm"]
    new = not os.path.exists(log_file)
    with open(log_file, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new:
            w.writeheader()
        for ip in ips:
            info = get_info(ip) or {}
            row = {k: info.get(k, "") for k in fields}
            row["timestamp"] = timestamp
            row["ip"] = ip
            w.writerow(row)
    print(f"Logged status snapshot to {log_file}")


def _delta(before, after):
    def val(info, key, fmt):
        return _num(info.get(key) if info else None, fmt)
    return ("err {eb} -> {ea} | F/CV {fb}/{cb} -> {fa}/{ca}").format(
        eb=val(before, "errorPercentage", ".1f"), ea=val(after, "errorPercentage", ".1f"),
        fb=val(before, "frequency", "d"), cb=val(before, "coreVoltage", "d"),
        fa=val(after, "frequency", "d"), ca=val(after, "coreVoltage", "d"))


def run_on_fleet(targets, global_args, timestamp):
    """Run the benchmark on each miner in turn, then show before/after and state.
    Returns the number of miners whose run failed."""
    snapshots = {}
    failures = 0
    for i, (ip, per_ip_args) in enumerate(targets, 1):
        args = global_args + per_ip_args
        print(f"\n{'=' * 8} [{i}/{len(targets)}] {ip}  {' '.join(args)} {'=' * 8}")
        before = get_info(ip)
        result = subprocess.run([sys.executable, "-u", SCRIPT, ip] + args)
        after = get_info(ip)
        snapshots[ip] = (before, after)
        if result.returncode != 0:
            failures += 1
            print(f"  ({ip} exited with code {result.returncode})")

    print("\nBefore -> after:")
    for ip, (before, after) in snapshots.items():
        print(f"  {ip:15} | {_delta(before, after)}")
    print_status([ip for ip, _ in targets], title="Fleet state after run")
    if failures:
        print(f"\n{failures} of {len(targets)} miner run(s) failed.")
    return failures


def load_targets(args):
    """List of (ip, per_ip_extra_args). --file lines may carry args after the IP."""
    targets = [(ip, []) for ip in args.ips]
    if args.file:
        with open(args.file) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                targets.append((parts[0], parts[1:]))
    seen = set()
    return [(ip, extra) for ip, extra in targets if not (ip in seen or seen.add(ip))]


def _now_stamp():
    # Local timestamp for logs; import here so the module imports cleanly under test.
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def main():
    p = argparse.ArgumentParser(description="Bitaxe fleet status / benchmark orchestrator")
    p.add_argument("ips", nargs="*", help="miner IP addresses")
    p.add_argument("--file", help="file with one IP per line (# comments; optional per-IP args after the IP)")
    p.add_argument("--run", metavar="ARGS",
                   help='run the benchmark on each IP with these args, e.g. "--mode refine --max-temp 68"')
    p.add_argument("--log", metavar="FILE", help="append a status snapshot per miner to this CSV")
    args = p.parse_args()

    targets = load_targets(args)
    if not targets:
        p.error("no IPs given (pass them positionally or via --file)")
    ips = [ip for ip, _ in targets]
    stamp = _now_stamp()

    if args.run is not None:
        failures = run_on_fleet(targets, args.run.split(), stamp)
        if args.log:
            log_status(ips, args.log, stamp)
        sys.exit(1 if failures else 0)
    else:
        print_status(ips)
        if args.log:
            log_status(ips, args.log, stamp)


if __name__ == "__main__":
    main()
