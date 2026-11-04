"""Check the live test's interface matching and terminal descriptor handling."""
import json
import os
from pathlib import Path
import pty
import subprocess
import sys
import unittest
from unittest.mock import patch

import interface_changes as live

ROOT = Path(__file__).resolve().parents[1]


class InterfaceChangesRunner(unittest.TestCase):
    def snapshot(self, names, blocked=False):
        return {"blocked": blocked, "interfaces": [{"name": name} for name in names],
                "bridges": {"501-mybox-net-test": "bridge100"}}

    def test_removed_bridge_name_does_not_match_another_bridge_prefix(self):
        snapshot = self.snapshot(["en0", "bridge100"])
        response = subprocess.CompletedProcess([], 0, stdout=json.dumps(snapshot))
        with patch.object(live, "sudo", return_value=response):
            self.assertEqual(live.wait_for_interface("bridge1", present=False, timeout=.1), snapshot)

    def test_interface_addition_requires_an_exact_name_and_a_healthy_snapshot(self):
        snapshots = [self.snapshot(["bridge100"]), self.snapshot(["bridge1"], blocked=True), self.snapshot(["bridge1"])]
        responses = [subprocess.CompletedProcess([], 0, stdout=json.dumps(item)) for item in snapshots]
        with patch.object(live, "sudo", side_effect=responses) as sudo:
            self.assertEqual(live.wait_for_interface("bridge1", present=True, timeout=1), snapshots[-1])
            self.assertEqual(sudo.call_count, 3)

    def test_timeout_identifies_the_interface_and_last_snapshot(self):
        response = subprocess.CompletedProcess([], 0, stdout=json.dumps(self.snapshot(["bridge1", "bridge100"])))
        with patch.object(live, "sudo", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "interface removal: bridge1.*Last snapshot:"):
                live.wait_for_interface("bridge1", present=False, timeout=.02)

    def test_child_commands_cannot_make_the_shared_terminal_nonblocking(self):
        master, slave = pty.openpty()
        script = r'''
import os, sys
sys.path.insert(0, 'tests')
import interface_changes as live
assert os.isatty(0) and os.isatty(1)
os.set_blocking(1, False)
live.prepare_terminal_output()
assert os.get_blocking(1), 'Terminal output was not restored.'
child = 'import os; os.set_blocking(0, False)'
live.run(sys.executable, '-c', child)
assert os.get_blocking(1), 'run() exposed the terminal input descriptor.'
live.BOX = sys.executable
live.box('-c', child)
assert os.get_blocking(1), 'box() exposed the terminal input descriptor.'
'''
        try:
            result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, stdin=slave, stdout=slave, stderr=subprocess.PIPE, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
        finally:
            os.close(master)
            os.close(slave)


if __name__ == "__main__":
    unittest.main()
