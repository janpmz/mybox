"""Check cleanup process detection using disposable executables, without root."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SUPPORT = ROOT/'scripts/uninstall-support.sh'


class UninstallProcesses(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='mybox-uninstall-test-')
        self.addCleanup(temporary.cleanup)
        self.runtime = Path(temporary.name)/'Application Support/MyBox/Runtime'
        self.binary = self.runtime/'bin/runtime-test'
        self.binary.parent.mkdir(parents=True)
        source = Path(temporary.name)/'wait.c'
        source.write_text('#include <stdlib.h>\n#include <unistd.h>\n'
                          'int main(int argc, char **argv) {\n'
                          '  usleep((unsigned int)(strtod(argv[1], 0) * 1000000));\n'
                          '  return 0;\n}\n')
        subprocess.run(['/usr/bin/xcrun', 'clang', str(source), '-o', str(self.binary)],
                       capture_output=True, check=True, timeout=30)

    def start(self, duration):
        process = subprocess.Popen([str(self.binary), str(duration)])
        def stop():
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
        self.addCleanup(stop)
        return process

    def check(self, operation, attempts='1'):
        # Pass paths as positional parameters, including spaces and shell text.
        return subprocess.run(['/bin/bash', '-c',
                               'set -euo pipefail; . "$1"; "$2" "$3" "$4"',
                               'test', str(SUPPORT), operation, str(self.runtime), attempts],
                              capture_output=True, text=True, timeout=15)

    def test_ignores_shell_arguments_that_mention_runtime(self):
        # The inspection shell itself contains the runtime path as an argument.
        response = self.check('mybox_runtime_processes')
        self.assertEqual(response.returncode, 0, response.stderr)
        self.assertEqual(response.stdout, '')

    def test_finds_real_runtime_executable_and_reports_timeout(self):
        process = self.start(10)
        response = self.check('mybox_runtime_processes')
        self.assertEqual(response.returncode, 0, response.stderr)
        self.assertEqual(response.stdout.strip(), f'{process.pid} {self.binary}')
        response = self.check('mybox_wait_for_runtime_exit')
        self.assertNotEqual(response.returncode, 0)
        self.assertIn(str(self.binary), response.stderr)
        self.assertIsNone(process.poll(), 'The cleanup check must not kill processes.')

    def test_waits_for_delayed_shutdown(self):
        process = self.start(0.4)
        response = self.check('mybox_wait_for_runtime_exit', '20')
        self.assertEqual(response.returncode, 0, response.stderr)
        self.assertEqual(process.wait(timeout=2), 0)


if __name__ == '__main__':
    unittest.main()
