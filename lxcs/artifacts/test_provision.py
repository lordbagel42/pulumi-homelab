import importlib.util
from pathlib import Path
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location("provision", Path(__file__).with_name("provision.py"))
provision = importlib.util.module_from_spec(SPEC)


class ProvisionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        SPEC.loader.exec_module(provision)

    def test_environment_excludes_bootstrap_password_and_escapes(self):
        env = provision.app_environment({"ownerEmail": "owner@example.com", "authSecret": "a'\"$\\b"})
        self.assertNotIn("ARTIFACTS_OWNER_PASSWORD", env)
        rendered = provision.environment_file(env)
        self.assertIn('BETTER_AUTH_SECRET="a\'\\"$\\\\b"', rendered)
        with self.assertRaises(ValueError):
            provision.environment_file({"BAD": "line\nbreak"})

    def test_route_and_health_do_not_rewrite_host_or_use_authentik(self):
        service = provision.consul_service("192.168.0.216")["service"]
        tags = service["tags"]
        self.assertIn("traefik.http.routers.artifacts.priority=200", tags)
        self.assertIn("traefik.http.services.artifacts.loadbalancer.passhostheader=true", tags)
        self.assertFalse(any("middlewares" in tag for tag in tags))
        self.assertEqual(service["check"]["header"], {"Host": ["artifacts.raygen.dev"]})
        self.assertIn("{0,52}", next(tag for tag in tags if ".rule=" in tag))

    def test_release_hash_mismatch_and_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "artifacts"
            source.write_bytes(b"native executable")
            with self.assertRaises(ValueError):
                provision.verify_binary(source, "0" * 64)
            link = Path(tmp) / "link"
            link.symlink_to(source)
            with self.assertRaises(ValueError):
                provision.verify_binary(link, "0" * 64)

    def test_release_remains_traversable_with_private_umask_and_is_not_overwritten(self):
        import hashlib
        import os
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "artifacts"
            source.write_bytes(b"native executable")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            old_mask = os.umask(0o077)
            try:
                release = provision.install_release(root, source, digest)
            finally:
                os.umask(old_mask)
            self.assertEqual(release.stat().st_mode & 0o777, 0o755)
            self.assertEqual(release.parent.stat().st_mode & 0o777, 0o755)
            self.assertEqual((release / "artifacts").stat().st_mode & 0o777, 0o755)
            before = (release / "artifacts").stat().st_mtime_ns
            provision.install_release(root, source, digest)
            self.assertEqual((release / "artifacts").stat().st_mtime_ns, before)


if __name__ == "__main__":
    unittest.main()
