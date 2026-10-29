import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "storage.py"
spec = importlib.util.spec_from_file_location("storage", SCRIPT)
storage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(storage)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        self.target = self.base / "target"
        self.source.mkdir()
        self.target.mkdir()
        self.config = {"roots": {"local": {"path": str(self.source)},
                                  "nas": {"path": str(self.target), "writable": True}}}
        self.config_path = self.base / "config.json"
        self.save_config()
        (self.source / "report.txt").write_bytes(b"test payload\x00\xff")

    def save_config(self):
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")

    def cli(self, *args):
        p = subprocess.run([sys.executable, str(SCRIPT), "--config", str(self.config_path), *args],
                           capture_output=True, text=True, timeout=10)
        return p.returncode, json.loads(p.stdout)

    def test_copy_preview_then_verified_write(self):
        args = ("copy", "--root", "local", "--path", "report.txt", "--to-root", "nas", "--to-path", "copy.txt")
        code, data = self.cli(*args)
        self.assertEqual(code, 0)
        self.assertFalse(data["applied"])
        self.assertFalse((self.target / "copy.txt").exists())
        code, data = self.cli(*args, "--apply")
        self.assertEqual(code, 0)
        self.assertTrue(data["verified"])
        self.assertEqual((self.source / "report.txt").read_bytes(), (self.target / "copy.txt").read_bytes())

    def test_existing_destination_preserved(self):
        destination = self.target / "copy.txt"
        destination.write_text("keep")
        with self.assertRaises(storage.StorageError):
            storage.copy_verified(self.source / "report.txt", destination, True)
        self.assertEqual(destination.read_text(), "keep")

    def test_readonly_root_rejected(self):
        with self.assertRaises(storage.StorageError):
            storage.resolve_path(self.config, "local", "new", write=True)

    def test_path_escape_and_windows_aliases_rejected(self):
        for path in ("../outside", "a/../../x", "/tmp/x", "C:foo", "C:/foo", "a:stream", "//host/share", "a/../b", "NUL.txt", "folder./x", "x ", "bad\x00name"):
            with self.subTest(path=path), self.assertRaises(storage.StorageError):
                storage.resolve_path(self.config, "nas", path)

    def test_missing_root_not_created(self):
        self.config["roots"]["nas"]["path"] = str(self.base / "missing")
        with self.assertRaises(storage.StorageError):
            storage.resolve_path(self.config, "nas", "new", True)
        self.assertFalse((self.base / "missing").exists())

    def test_search_limits_and_case(self):
        (self.source / "nested").mkdir()
        (self.source / "nested" / "UPPER.PDF").write_text("x")
        result = storage.scan(self.source, "*.pdf", 20, 100, True)
        self.assertEqual(result["items"][0]["path"], "nested/UPPER.PDF")
        self.assertFalse(result["truncated"])
        result = storage.scan(self.source, "*", 1, 100, True)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["items"]), 1)
        self.assertTrue(storage.scan(self.source, "nomatch", 10, 1, True)["truncated"])

    def test_symlink_escape(self):
        link = self.source / "redirect"
        try:
            link.symlink_to(self.target, target_is_directory=True)
        except OSError:
            self.skipTest("OS does not allow creating symlinks")
        with self.assertRaises(storage.StorageError):
            storage.resolve_path(self.config, "local", "redirect/x")
        self.assertEqual(storage.scan(self.source, "*", 100, 100, True)["skipped"], 1)

    def test_reparse_flag_rejected(self):
        with patch.object(storage, "is_link", return_value=True):
            with self.assertRaises(storage.StorageError):
                storage.resolve_path(self.config, "local", "report.txt")

    def test_verify_mismatch_exit(self):
        (self.target / "other.txt").write_text("other")
        code, data = self.cli("verify", "--root", "local", "--path", "report.txt", "--to-root", "nas", "--to-path", "other.txt")
        self.assertEqual(code, 1)
        self.assertFalse(data["verified"])

    def test_source_change_keeps_source(self):
        with patch.object(storage, "digest", return_value="changed"):
            with self.assertRaises(storage.StorageError):
                storage.copy_verified(self.source / "report.txt", self.target / "copy", True)
        self.assertTrue((self.source / "report.txt").exists())
        self.assertTrue((self.target / "copy").exists())

    def test_status_not_disk_health(self):
        code, data = self.cli("status", "--root", "nas")
        self.assertEqual(code, 0)
        self.assertGreater(data["free_bytes"], 0)
        self.assertEqual(data["disk_health"], "unknown")

    def test_sync_status_freshness_and_error_redaction(self):
        status = self.base / "status.json"
        status.write_text(json.dumps({"phase": "idle", "errors": ["private details"]}))
        os.utime(status, (1, 1))
        self.config["sync_status_file"] = str(status)
        self.save_config()
        code, data = self.cli("sync-status")
        self.assertEqual(code, 0)
        self.assertTrue(data["stale"])
        self.assertEqual(data["error_count"], 1)
        self.assertNotIn("private details", json.dumps(data))

    def test_invalid_config_clean_failure(self):
        self.config_path.write_text("{broken")
        code, data = self.cli("status", "--root", "nas")
        self.assertEqual(code, 2)
        self.assertFalse(data["ok"])
        self.assertNotIn(str(self.base), json.dumps(data))

    def test_mkdir_preview_and_apply(self):
        args = ("mkdir", "--root", "nas", "--path", "nested/folder")
        self.assertFalse(self.cli(*args)[1]["applied"])
        self.assertFalse((self.target / "nested").exists())
        self.assertTrue(self.cli(*args, "--apply")[1]["applied"])
        self.assertTrue((self.target / "nested" / "folder").is_dir())

    def test_mkdir_rejects_file_in_preview_and_apply(self):
        (self.target / 'blocked').write_bytes(b'preserve')
        for path in ('blocked', 'blocked/child'):
            for flags in ((), ('--apply',)):
                code, data = self.cli('mkdir', '--root', 'nas', '--path', path, *flags)
                self.assertEqual(code, 2)
                self.assertFalse(data['ok'])
        self.assertEqual((self.target / 'blocked').read_bytes(), b'preserve')

    def test_sync_status_requires_array(self):
        status = self.base / 'status.json'
        self.config['sync_status_file'] = str(status)
        self.save_config()
        for value in ('private details', {'private details': 1}, None):
            status.write_text(json.dumps({'errors': value}))
            code, data = self.cli('sync-status')
            self.assertEqual(code, 2)
            self.assertFalse(data['ok'])
            self.assertNotIn('private details', json.dumps(data))

    def test_timeout_report(self):
        with patch.object(sys, "argv", [str(SCRIPT), "status", "--root", "nas"]), \
             patch.object(storage.subprocess, "run", side_effect=subprocess.TimeoutExpired("worker", 1)), \
             patch.object(storage, "emit") as emit:
            self.assertEqual(storage.main(), 2)
            self.assertFalse(emit.call_args.args[0]["ok"])


if __name__ == "__main__":
    unittest.main()
