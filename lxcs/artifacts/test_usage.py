import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location("usage", Path(__file__).with_name("usage.py"))
usage = importlib.util.module_from_spec(SPEC)


class UsageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        SPEC.loader.exec_module(usage)

    def fixture(self, root):
        values = {
            "memory.current": "900", "memory.peak": "1000", "memory.max": "1024",
            "memory.stat": "anon 400\nfile 450\ninactive_file 300\n",
            "memory.events": "oom 0\noom_kill 0\nhigh 2\n",
            "memory.swap.current": "0", "memory.swap.max": "max",
            "cpu.stat": "usage_usec 1000000\nnr_throttled 2\nthrottled_usec 100\n",
            "cpu.max": "100000 100000",
            "cpu.pressure": "some avg10=0.00 avg60=0.01 avg300=0.02 total=12\n",
            "memory.pressure": "some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n",
        }
        for key, value in values.items():
            (root / key).write_text(value)

    def test_cgroup_accounting_preserves_anon_and_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            sample = usage.collect(root, root, timestamp=100, monotonic=20, systemd=False)
        self.assertEqual(sample["memory"]["current"], 900)
        self.assertEqual(sample["memory"]["working_set"], 600)
        self.assertEqual(sample["memory"]["stat"]["anon"], 400)
        self.assertEqual(sample["memory"]["stat"]["file"], 450)
        self.assertEqual(sample["cpu"]["max"], {"quota_usec": 100000, "period_usec": 100000})
        self.assertEqual(sample["swap"]["max"], "max")
        self.assertIsNone(sample["pressure"]["io"])
        self.assertTrue(any("io.pressure" in error for error in sample["errors"]))
        self.assertGreater(sample["disk"]["available_bytes"], 0)

    def test_missing_measurements_are_null_not_host_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            sample = usage.collect(Path(tmp), Path(tmp), systemd=False)
        self.assertIsNone(sample["memory"]["current"])
        self.assertIsNone(sample["memory"]["working_set"])
        self.assertIsNone(sample["cpu"]["stat"])
        self.assertIsNone(sample["uptime_seconds"])

    def sample(self, timestamp, counter, memory=600):
        return {"timestamp": timestamp, "monotonic": timestamp,
                "memory": {"current": memory + 300, "working_set": memory,
                           "events": {"oom": counter // 1000000, "oom_kill": 0}},
                "cpu": {"stat": {"usage_usec": counter, "nr_throttled": 0,
                                  "throttled_usec": 0}},
                "disk": {"available_bytes": 1000, "available_inodes": 50},
                "errors": []}

    def test_report_rates_gaps_reset_and_nearest_rank_p95(self):
        samples = [self.sample(0, 0), self.sample(60, 30000000),
                   self.sample(180, 60000000, 800), self.sample(240, 5)]
        report = usage.report(samples)
        self.assertEqual(report["coverage"]["missing_intervals"], 1)
        self.assertEqual(report["coverage"]["span_seconds"], 240)
        self.assertEqual(report["cpu_cores"]["peak"], 0.5)
        self.assertEqual(report["cpu_cores"]["intervals"], 2)
        self.assertEqual(report["counter_resets"]["cpu.usage_usec"], 1)
        self.assertEqual(report["memory_working_set_bytes"], {"peak": 800, "p95": 800, "samples": 4})
        self.assertEqual(report["memory_total_bytes"]["peak"], 1100)
        self.assertEqual(report["event_deltas"]["memory.oom"], 60)

    def test_report_empty_duplicate_time_and_missing_counter(self):
        self.assertIsNone(usage.report([])["memory_total_bytes"]["peak"])
        report = usage.report([self.sample(0, 0), self.sample(0, 3)])
        self.assertEqual(report["cpu_cores"]["intervals"], 0)
        self.assertEqual(report["coverage"]["invalid_intervals"], 1)

    def test_hidden_ancestor_limits_require_host_verification(self):
        samples = [self.sample(0, 0), self.sample(60, 1000)]
        for sample in samples:
            sample["memory"]["max"] = "max"
            sample["cpu"]["max"] = {"quota_usec": "max", "period_usec": 100000}
        report = usage.report(samples)
        self.assertEqual(report["limits"]["host_verification_required"], ["memory", "cpu"])
        self.assertEqual(report["limits"]["namespace_memory_max"], "max")
        self.assertEqual(report["limits"]["namespace_cpu_max"]["quota_usec"], "max")
        self.assertEqual(report["memory_working_set_bytes"]["peak"], 600)

    def test_report_pressure_totals_initial_oom_and_exact_p95_boundary(self):
        samples = [self.sample(i * 60, i * 1000000, i) for i in range(1, 21)]
        for i, sample in enumerate(samples):
            sample["pressure"] = {"memory": {"some": {"avg10": 0, "total": i * 10}}}
        report = usage.report(samples)
        self.assertEqual(report["memory_working_set_bytes"]["p95"], 19)
        self.assertEqual(report["event_deltas"]["pressure.memory.some"], 190)
        self.assertEqual(report["memory_event_peaks"]["oom"], 20)
        self.assertIsNone(usage.report([])["event_deltas"]["memory.oom"])

    def test_report_reboot_invalidates_even_increasing_cpu_counter(self):
        previous, current = self.sample(0, 10), self.sample(60, 100)
        previous["uptime_seconds"], current["uptime_seconds"] = 100, 5
        report = usage.report([previous, current])
        self.assertEqual(report["cpu_cores"]["intervals"], 0)
        self.assertEqual(report["counter_resets"]["cpu.usage_usec"], 1)

    def test_retention_boundary_permissions_and_size_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            now = 20 * 86400
            usage.persist({"timestamp": now}, root)
            expired = root / "1970-01-06.jsonl"
            boundary = root / "1970-01-07.jsonl"
            expired.write_text("old\n")
            boundary.write_text("keep\n")
            usage.persist({"timestamp": now}, root)
            self.assertFalse(expired.exists())
            self.assertTrue(boundary.exists())
            current = root / "1970-01-21.jsonl"
            self.assertEqual(current.stat().st_mode & 0o777, 0o600)
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            self.assertEqual(len(current.read_text().splitlines()), 2)
            with self.assertRaises(ValueError):
                usage.persist({"timestamp": now, "large": "x" * 100}, root, max_bytes=10)


if __name__ == "__main__":
    unittest.main()
