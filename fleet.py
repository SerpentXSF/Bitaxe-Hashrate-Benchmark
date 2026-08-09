#!/usr/bin/env python3
"""Fleet helper for the Bitaxe benchmark.

Two things:

1. Status (default): read /api/system/info from each miner and print a compact
   table (hashrate, error, temp, VR, fan, J/TH, uptime). Fast and read-only.

2. Run (`--run "<args>"`): run bitaxe_hashrate_benchmark.py on each miner in
   turn with the given args (streaming its output), then print the resulting
   state of every miner. Use it to tune or check a whole fleet in one command.

Examples:
  python fleet.py 192.168.2.10 192.168.2.11
  python fleet.py --file miners.txt
  python fleet.py 192.168.2.10 192.168.2.11 --run "--check --max-temp 68"
  python fleet.py --file miners.txt --run "--mode refine --max-temp 68 --benchmark-time 300"
"""
import argparse
import os
import subprocess
import sys

import requests

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bitaxe_hashrate_benchmark.py")


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


HEADER = ("  {:15} | {:12} | {:>7} | {:>6} | {:>6} | {:>5} | {:>4} | {:>10} | {:>8} | {:>5}"
          .format("ip", "host", "F/CV", "hash", "err", "temp", "vr", "fan", "J/TH", "up"))


def status_row(ip, info):
    if info is None:
        return f"  {ip:15} | UNREACHABLE"
    jth = efficiency_jth(info)
    fan_pct = info.get("fanspeed")
    fan_rpm = info.get("fanrpm")
    fan = f"{fan_pct:.0f}%/{fan_rpm:.0f}" if (fan_pct is not None and fan_rpm is not None) else "n/a"
    return ("  {ip:15} | {host:12.12} | {f:>4}/{cv:<4} | {hr:>5.0f}G | {err:>5.1f}% | "
            "{t:>4.1f} | {vr:>4.0f} | {fan:>10} | {jth:>7} | {up:>4.0f}m").format(
        ip=ip,
        host=info.get("hostname", "") or "",
        f=info.get("frequency", 0),
        cv=info.get("coreVoltage", 0),
        hr=info.get("hashRate", 0) or 0,
        err=info.get("errorPercentage", 0) or 0,
        t=info.get("temp", 0) or 0,
        vr=info.get("vrTemp", 0) or 0,
        fan=fan,
        jth=f"{jth:.2f}" if jth is not None else "n/a",
        up=(info.get("uptimeSeconds", 0) or 0) / 60,
    )


def print_status(ips, title="Fleet status"):
    print(f"\n{title}:")
    print(HEADER)
    for ip in ips:
        print(status_row(ip, get_info(ip)))


def run_on_fleet(ips, extra_args):
    """Run the benchmark on each miner in turn, then show the resulting state."""
    for i, ip in enumerate(ips, 1):
        print(f"\n{'=' * 8} [{i}/{len(ips)}] {ip}  {' '.join(extra_args)} {'=' * 8}")
        result = subprocess.run([sys.executable, "-u", SCRIPT, ip] + extra_args)
        if result.returncode != 0:
            print(f"  ({ip} exited with code {result.returncode})")
    print_status(ips, title="Fleet state after run")


def load_ips(args):
    ips = list(args.ips)
    if args.file:
        with open(args.file) as f:
            ips += [line.strip() for line in f if line.strip() and not line.startswith("#")]
    # de-duplicate, keep order
    seen = set()
    return [ip for ip in ips if not (ip in seen or seen.add(ip))]


def main():
    p = argparse.ArgumentParser(description="Bitaxe fleet status / benchmark orchestrator")
    p.add_argument("ips", nargs="*", help="miner IP addresses")
    p.add_argument("--file", help="file with one IP per line (# comments allowed)")
    p.add_argument("--run", metavar="ARGS",
                   help='run the benchmark on each IP with these args, '
                        'e.g. "--mode refine --max-temp 68"')
    args = p.parse_args()

    ips = load_ips(args)
    if not ips:
        p.error("no IPs given (pass them positionally or via --file)")

    if args.run is not None:
        run_on_fleet(ips, args.run.split())
    else:
        print_status(ips)


if __name__ == "__main__":
    main()
