#!/usr/bin/env python3
"""Container cgroup-v2 sampling and offline reporting; never changes sizing."""

import argparse
from collections import Counter
from datetime import datetime, timezone, timedelta
import fcntl
import json
import math
import os
from pathlib import Path
import subprocess
import time


def key_values(text):
    return {key: int(value) for key, value in (line.split() for line in text.splitlines())}


def limit(text):
    return "max" if text.strip() == "max" else int(text)


def pressure(text):
    return {parts[0]: {key: (int(value) if key == "total" else float(value))
                       for key, value in (field.split("=") for field in parts[1:])}
            for parts in (line.split() for line in text.splitlines())}


def collect(cgroup=Path("/sys/fs/cgroup"), disk=Path("/var/lib/artifacts"),
            timestamp=None, monotonic=None, systemd=True):
    """Read the LXC namespace ROOT, not this service's child cgroup or /proc RAM.

    Limits may live in an ancestor outside this namespace. A visible 'max'
    is not proof of no inherited limit; host counters must confirm pressure.
    Unavailable files stay null; never substitute host-global /proc totals.
    """
    errors = []

    def read(name, parse=int):
        try:
            return parse((cgroup / name).read_text())
        except (OSError, ValueError, IndexError) as error:
            errors.append(f"{name}: {type(error).__name__}")
            return None

    memory = {key: read("memory." + key, key_values if key in ("stat", "events") else limit)
              for key in ("current", "peak", "max", "stat", "events")}
    memory["working_set"] = None
    if memory["current"] is not None and memory["stat"] is not None:
        inactive = memory["stat"].get("inactive_file")
        if inactive is not None:
            memory["working_set"] = max(0, memory["current"] - inactive)
    sample = {
        "schema": 1, "timestamp": time.time() if timestamp is None else timestamp,
        "monotonic": time.monotonic() if monotonic is None else monotonic,
        "cgroup": str(cgroup), "memory": memory,
        "swap": {key: read("memory.swap." + key, limit) for key in ("current", "max")},
        "cpu": {"stat": read("cpu.stat", key_values),
                "max": read("cpu.max", lambda text: dict(zip(
                    ("quota_usec", "period_usec"), map(limit, text.split()))))},
        "pressure": {key: read(key + ".pressure", pressure) for key in ("cpu", "memory", "io")},
        "disk": None, "uptime_seconds": None, "app": None, "errors": errors,
    }
    try:
        stat = os.statvfs(disk)
        sample["disk"] = {"total_bytes": stat.f_blocks * stat.f_frsize,
                          "available_bytes": stat.f_bavail * stat.f_frsize,
                          "free_bytes": stat.f_bfree * stat.f_frsize,
                          "total_inodes": stat.f_files, "available_inodes": stat.f_favail}
    except OSError as error:
        errors.append(f"statvfs: {type(error).__name__}")
    if systemd:
        try:
            result = subprocess.run(["systemctl", "show", "artifacts.service",
                                     "-p", "NRestarts", "-p", "ActiveState", "-p", "SubState",
                                     "-p", "Result"], check=True, capture_output=True, text=True, timeout=10)
            sample["app"] = dict(line.split("=", 1) for line in result.stdout.splitlines())
            # systemd manager start, not host /proc/uptime or boot_id.
            start = subprocess.run(["systemctl", "show", "-p", "UserspaceTimestampMonotonic", "--value"],
                                   check=True, capture_output=True, text=True, timeout=10)
            started = int(start.stdout.strip()) / 1e6
            sample["uptime_seconds"] = max(0, sample["monotonic"] - started) if started else None
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            errors.append(f"systemd: {type(error).__name__}")
    return sample


def persist(sample, directory=Path("/var/log/artifacts-usage"), max_bytes=16 * 1024 * 1024):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    today = datetime.fromtimestamp(sample["timestamp"], timezone.utc).date()
    for old in directory.glob("????-??-??.jsonl"):
        try:
            day = datetime.strptime(old.stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if day < today - timedelta(days=14):
            old.unlink()
    content = (json.dumps(sample, separators=(",", ":"), allow_nan=False) + "\n").encode()
    target = directory / f"{today.isoformat()}.jsonl"
    fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "ab") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        os.fchmod(stream.fileno(), 0o600)
        if os.fstat(stream.fileno()).st_size + len(content) > max_bytes:
            raise ValueError("daily usage log size cap reached")
        stream.write(content)


def summary(values):
    values = sorted(value for value in values if isinstance(value, (int, float)))
    return {"peak": max(values) if values else None,
            "p95": values[math.ceil(len(values) * .95) - 1] if values else None,
            "samples": len(values)}


def counters(sample):
    result = {}
    for section, field in (("cpu", "stat"), ("memory", "events")):
        for key, value in (sample.get(section, {}).get(field) or {}).items():
            result[f"{section}.{key}"] = value
    for resource, kinds in sample.get("pressure", {}).items():
        for kind, values in (kinds or {}).items():
            if values.get("total") is not None:
                result[f"pressure.{resource}.{kind}"] = values["total"]
    return result


def report(samples):
    samples = sorted(samples, key=lambda sample: sample["timestamp"])
    gaps, rates, invalid = [], [], 0
    resets, deltas, observed = Counter(), Counter(), Counter()
    errors = Counter(error for sample in samples for error in sample.get("errors", []))
    for previous, current in zip(samples, samples[1:]):
        wall = current["timestamp"] - previous["timestamp"]
        elapsed = current["monotonic"] - previous["monotonic"]
        if wall > 90:
            gaps.append({"after": previous["timestamp"], "seconds": wall,
                         "missing_intervals": max(0, round(wall / 60) - 1)})
        if elapsed <= 0 or wall <= 0:
            invalid += 1
        # Recreated container: manager uptime decreases even if host monotonic doesn't.
        reboot = (current.get("uptime_seconds") is not None and previous.get("uptime_seconds") is not None
                  and current["uptime_seconds"] < previous["uptime_seconds"])
        old, new = counters(previous), counters(current)
        for name in new.keys() & old.keys():
            delta = new[name] - old[name]
            if delta < 0 or elapsed <= 0 or reboot:
                resets[name] += 1
                continue
            if wall <= 0:
                continue
            deltas[name] += delta
            observed[name] += 1
            if name == "cpu.usage_usec":
                rates.append(delta / 1e6 / elapsed)
    total = summary(sample.get("memory", {}).get("current") for sample in samples)
    working = summary(sample.get("memory", {}).get("working_set") for sample in samples)
    disk = [sample["disk"] for sample in samples if sample.get("disk")]
    cpu = summary(rates)
    cpu["intervals"] = cpu.pop("samples")
    pressure_peaks = {}
    for resource in ("cpu", "memory", "io"):
        pressure_peaks[resource] = {kind: summary(
            ((sample.get("pressure", {}).get(resource) or {}).get(kind) or {}).get("avg10")
            for sample in samples)["peak"] for kind in ("some", "full")}
    required_counters = {"cpu.usage_usec", "cpu.nr_throttled", "cpu.throttled_usec",
                         "memory.oom", "memory.oom_kill", "memory.high", "memory.max"}
    required_counters.update(f"pressure.{resource}.{kind}" for resource in ("cpu", "memory", "io")
                             for kind in ("some", "full"))
    event_deltas = {name: deltas[name] if observed[name] else None
                    for name in sorted(required_counters | deltas.keys())}
    latest = samples[-1] if samples else {}
    hidden = []
    if not samples or any(sample.get("memory", {}).get("max") in (None, "max") for sample in samples):
        hidden.append("memory")
    if not samples or any((sample.get("cpu", {}).get("max") or {}).get("quota_usec") in (None, "max")
                          for sample in samples):
        hidden.append("cpu")
    return {
        "limits": {"namespace_memory_max": latest.get("memory", {}).get("max"),
                   "namespace_cpu_max": latest.get("cpu", {}).get("max"),
                   "host_verification_required": hidden},
        "coverage": {"samples": len(samples),
                     "span_seconds": samples[-1]["timestamp"] - samples[0]["timestamp"] if samples else 0,
                     "first": samples[0]["timestamp"] if samples else None,
                     "last": samples[-1]["timestamp"] if samples else None,
                     "gaps": gaps, "missing_intervals": sum(gap["missing_intervals"] for gap in gaps),
                     "invalid_intervals": invalid},
        "memory_total_bytes": total, "memory_working_set_bytes": working,
        "memory_kernel_peak_bytes": summary(sample.get("memory", {}).get("peak") for sample in samples),
        "cpu_cores": cpu, "counter_resets": dict(resets),
        "event_deltas": event_deltas, "counter_intervals": dict(observed),
        "memory_event_peaks": {key: summary((sample.get("memory", {}).get("events") or {}).get(key)
                                            for sample in samples)["peak"]
                               for key in ("oom", "oom_kill", "high", "max")},
        "pressure_avg10_peak_percent": pressure_peaks,
        "disk_headroom": {key: min((item[key] for item in disk), default=None)
                          for key in ("available_bytes", "available_inodes")},
        "app_latest": samples[-1].get("app") if samples else None,
        "errors": dict(errors),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("sample", "report"))
    parser.add_argument("--directory", type=Path, default=Path("/var/log/artifacts-usage"))
    parser.add_argument("--hours", type=float, default=72, help="report trailing window (default 72h)")
    args = parser.parse_args()
    if args.action == "sample":
        persist(collect(), args.directory)
        return
    now = time.time()
    samples, malformed = [], 0
    for path in sorted(args.directory.glob("????-??-??.jsonl")):
        with path.open() as stream:
            for line in stream:
                try:
                    sample = json.loads(line)
                    if now - args.hours * 3600 <= sample["timestamp"] <= now:
                        if not isinstance(sample.get("monotonic"), (int, float)):
                            raise ValueError("missing monotonic time")
                        samples.append(sample)
                except (ValueError, KeyError, TypeError):
                    malformed += 1
    result = report(samples)
    result["malformed_lines"] = malformed
    result["requested_hours"] = args.hours
    result["coverage"]["start_gap_seconds"] = max(0, samples[0]["timestamp"] - (now - args.hours * 3600)) if samples else args.hours * 3600
    result["coverage"]["end_gap_seconds"] = max(0, now - samples[-1]["timestamp"]) if samples else args.hours * 3600
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
