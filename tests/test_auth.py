"""Check terminal ownership and hidden input without passwords or real sudo."""
import errno
import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import tempfile
import termios
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
BOX = str(ROOT / '.build/mybox-test')
DUMMY = b'MYBOX-DUMMY-INPUT-NOT-A-PASSWORD'

PROMPT = r'''#!/usr/bin/python3
import os, sys, termios
fd = os.open('/dev/tty', os.O_RDWR)
if os.getpgrp() != os.tcgetpgrp(fd):
    os.write(fd, b'BACKGROUND_AUTH_PROCESS\n')
    sys.exit(81)
original = termios.tcgetattr(fd)
hidden = list(original)
hidden[3] &= ~termios.ECHO
try:
    termios.tcsetattr(fd, termios.TCSAFLUSH, hidden)
    os.write(fd, b'Password: ')
    while not os.read(fd, 1) in (b'\n', b'\r', b''):
        pass
finally:
    termios.tcsetattr(fd, termios.TCSAFLUSH, original)
    os.close(fd)
print('INPUT_COMPLETE')
sys.exit(int(os.environ.get('MYBOX_AUTH_EXIT', '0')))
'''


class Authentication(unittest.TestCase):
    def check_prompt(self, exit_code):
        with tempfile.TemporaryDirectory(prefix='mybox-auth-') as temporary:
            helper = Path(temporary) / 'fake-sudo'
            helper.write_text(PROMPT)
            helper.chmod(0o755)
            environment = dict(os.environ, MYBOX_TEST_HOST=str(helper), MYBOX_AUTH_EXIT=str(exit_code))
            child, master = pty.fork()
            if child == 0:
                # Reproduce the test's redirected stdin and captured stdout.
                result = subprocess.run([BOX, '_test-auth'], env=environment,
                                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE)
                os.write(1, result.stdout)
                os._exit(result.returncode)
            output = bytearray()
            status = None
            input_sent = False
            hidden = False
            original = termios.tcgetattr(master)
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    readable, _, _ = select.select([master], [], [], .05)
                    if readable:
                        try:
                            data = os.read(master, 4096)
                            output.extend(data)
                        except OSError as error:
                            if error.errno != errno.EIO:
                                raise
                    if b'Password: ' in output and not input_sent:
                        hidden = not (termios.tcgetattr(master)[3] & termios.ECHO)
                        self.assertTrue(hidden, 'The prompt left terminal echo enabled.')
                        os.write(master, DUMMY + b'\n')
                        input_sent = True
                    done, result_status = os.waitpid(child, os.WNOHANG)
                    if done:
                        status = result_status
                        break
                self.assertIsNotNone(status, 'The authentication process did not finish.')
                self.assertTrue(input_sent, output.decode(errors='replace'))
                self.assertNotIn(b'BACKGROUND_AUTH_PROCESS', output)
                self.assertNotIn(DUMMY, output)
                self.assertEqual(termios.tcgetattr(master)[3] & termios.ECHO, original[3] & termios.ECHO)
                self.assertEqual(os.WEXITSTATUS(status), exit_code)
            finally:
                if status is None:
                    os.killpg(child, signal.SIGKILL)
                    os.waitpid(child, 0)
                os.close(master)

    def test_redirected_caller_keeps_authentication_foreground_and_input_hidden(self):
        self.check_prompt(0)

    def test_failed_authentication_restores_terminal_echo(self):
        self.check_prompt(1)


if __name__ == '__main__':
    unittest.main()
