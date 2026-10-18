"""The release-tag repair may change workspace versions, not dependencies."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

VERIFY = Path(__file__).resolve().parents[1] / "verify-codex-lock.py"
LOCK = """version = 4

[[package]]
name = "codex-core"
version = "0.0.0"
dependencies = ["serde"]

[[package]]
name = "serde"
version = "1.0.228"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "0123456789abcdef"

[[package]]
name = "tungstenite"
version = "0.28.0"
source = "git+https://github.com/openai-oss-forks/tungstenite-rs?rev=abc#abc"
"""


class LockVerificationTests(unittest.TestCase):
    def verify(self, resolved):
        with tempfile.TemporaryDirectory() as directory:
            before = Path(directory) / "original.lock"
            after = Path(directory) / "resolved.lock"
            before.write_text(LOCK)
            after.write_text(resolved)
            return subprocess.run(
                [sys.executable, str(VERIFY), str(before), str(after)],
                capture_output=True,
                text=True,
                check=False,
            )

    def test_workspace_version_repair_is_allowed(self):
        result = self.verify(LOCK.replace('version = "0.0.0"', 'version = "0.157.1"'))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_dependency_changes_are_rejected(self):
        changes = [
            LOCK.replace('version = "1.0.228"', 'version = "1.0.229"'),
            LOCK.replace('checksum = "0123456789abcdef"', 'checksum = "changed"'),
            LOCK.replace("?rev=abc#abc", "?rev=def#def"),
            LOCK[: LOCK.index('[[package]]\nname = "tungstenite"')],
        ]
        for changed in changes:
            with self.subTest(lock=changed):
                result = self.verify(changed)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("non-workspace dependencies changed", result.stderr)


if __name__ == "__main__":
    unittest.main()
