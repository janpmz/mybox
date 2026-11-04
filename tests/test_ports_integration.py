"""Check live-test reporting and cleanup without changing real boxes or PF."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import ports_integration as runner


class LivePortRunner(unittest.TestCase):
    def test_each_completed_check_is_saved_immediately(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(runner, "REPORT", Path(temporary) / "report.json"):
            report = {"success": False}
            checks = report["checks"] = runner.Checks(report)
            checks["first"] = True
            saved = json.loads(runner.REPORT.read_text())
            self.assertEqual(saved["checks"], {"first": True})
            self.assertFalse(saved["success"])

    def test_cleanup_failure_does_not_hide_the_original_error_or_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "report.json"
            info = {"configuration": {"publishedPorts": []}, "status": {
                "startedDate": "test-boot", "networks": [{"ipv4Address": "192.168.66.3/24"}]}}

            def box(*args, **kwargs):
                if args[0] == "stop":
                    raise RuntimeError("simulated cleanup failure")
                return b""

            with patch.object(runner, "REPORT", report), patch.object(runner.os, "geteuid", return_value=501), \
                 patch.object(runner, "running", return_value={"existing": "unchanged"}), \
                 patch.object(runner, "run", return_value=b"unchanged"), patch.object(runner, "box", side_effect=box), \
                 patch.object(runner, "start_server"), patch.object(runner, "inspect", return_value=info), \
                 patch.object(runner, "connect_fails", return_value=True), \
                 patch.object(runner, "free_port", side_effect=[50001, 50002]), \
                 patch.object(runner, "page", side_effect=AssertionError("original HTTP failure")), \
                 patch.object(runner.shutil, "rmtree"):
                with self.assertRaisesRegex(AssertionError, "original HTTP failure"):
                    runner.main()
            saved = json.loads(report.read_text())
            self.assertFalse(saved["success"])
            self.assertIn("original HTTP failure", saved["error"])
            self.assertEqual(saved["cleanup"]["stop_box"], "simulated cleanup failure")
            self.assertTrue(saved["cleanup"]["delete_box"])
            self.assertTrue(saved["checks"]["existing_boxes_kept_running"])

    def test_echo_accepts_partial_tcp_reads(self):
        client = Mock()
        client.recv.side_effect = [b"ab", b"c", b"\n"]
        runner.echo(client, b"abc\n")
        client.sendall.assert_called_once_with(b"abc\n")


if __name__ == "__main__":
    unittest.main()
