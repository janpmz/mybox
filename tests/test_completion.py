"""Check zsh Tab completion with fake boxes and a real terminal."""
import errno
import json
import os
import pty
import select
import shlex
import signal
import time
import unittest

import test_cli


class Completion(unittest.TestCase):
    setUp = test_cli.CLI.setUp
    run_box = test_cli.CLI.run_box
    calls = test_cli.CLI.calls

    def save_names(self, *names):
        records = {"mybox-" + name: {
            "id": "mybox-" + name,
            "configuration": {"labels": {"com.mybox.managed": "true"}},
            "status": {"state": "stopped"},
        } for name in names}
        records["mybox-foreign"] = {
            "id": "mybox-foreign", "configuration": {"labels": {}}, "status": {"state": "running"},
        }
        records["mybox-helper-test"] = {
            "id": "mybox-helper-test", "configuration": {"labels": {"com.mybox.helper": "true"}},
            "status": {"state": "running"},
        }
        self.log.with_suffix(".state").write_text(json.dumps(records))

    def test_script_needs_no_backend_and_rejects_unsupported_shells(self):
        script = self.run_box("completion", "zsh").stdout
        self.assertIn("compdef _mybox mybox", script)
        for arguments in (("completion",), ("completion", "bash"), ("completion", "zsh", "extra"),
                          ("_complete-boxes", "extra")):
            self.run_box(*arguments, success=False)
        self.assertEqual(self.calls(), [])

    def test_candidates_only_list_managed_names(self):
        self.save_names("web", "document-player-box")
        self.assertEqual(self.run_box("_complete-boxes").stdout, "document-player-box\nweb\n")
        self.assertEqual(self.calls(), [["list", "--all", "--format", "json"]])
        self.save_names()
        self.assertEqual(self.run_box("_complete-boxes").stdout, "")

    def start_shell(self):
        (self.directory / "mybox").symlink_to(test_cli.BOX)
        script = self.directory / "completion.zsh"
        script.write_text(self.run_box("completion", "zsh").stdout)
        setup = self.directory / "setup.zsh"
        setup.write_text("PROMPT='MYBOX_READY> '\nRPROMPT=''\nbindkey -e\n"
                         + "source " + shlex.quote(str(script)) + "\n"
                         + "_mybox_test_report() { print -r -- \"MYBOX_BUFFER:$BUFFER\"; BUFFER=''; zle reset-prompt; }\n"
                         + "zle -N _mybox_test_report\nbindkey '^X' _mybox_test_report\n"
                         + "print -r -- MYBOX_INIT_DONE\n")
        child, master = pty.fork()
        if child == 0:
            os.execve("/bin/zsh", ["zsh", "-f", "-i"],
                      dict(self.environment, TERM="xterm", ZDOTDIR=str(self.directory)))
        self.child, self.master = child, master
        self.addCleanup(self.stop_shell)
        os.write(master, ("source " + shlex.quote(str(setup)) + "\n").encode())
        self.read_until(b"MYBOX_INIT_DONE\r\n")

    def stop_shell(self):
        try:
            os.write(self.master, b"exit\n")
            for attempt in range(2):
                if attempt:
                    os.kill(self.child, signal.SIGKILL)
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    if os.waitpid(self.child, os.WNOHANG)[0]:
                        return
                    time.sleep(.02)
            self.fail("The temporary zsh process did not exit.")
        finally:
            os.close(self.master)

    def read_until(self, marker):
        output = bytearray()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if select.select([self.master], [], [], .1)[0]:
                try:
                    data = os.read(self.master, 65536)
                except OSError as error:
                    if error.errno != errno.EIO:
                        raise
                    break
                if not data:
                    break
                output.extend(data)
                if marker in output:
                    return output.decode(errors="replace")
        self.fail("Missing terminal marker: " + output.decode(errors="replace"))

    def complete(self, command):
        os.write(self.master, (command + "\t\x18").encode())
        output = self.read_until(b"MYBOX_BUFFER:")
        if "\r\n" not in output.split("MYBOX_BUFFER:", 1)[1]:
            output += self.read_until(b"\r\n")
        return output.split("MYBOX_BUFFER:", 1)[1].split("\r\n", 1)[0], output

    def test_tab_completes_commands_options_and_partial_names_without_entering(self):
        self.save_names("document-player-box", "web")
        self.start_shell()
        cases = {
            "mybox ent": "mybox enter ",
            "mybox enter doc": "mybox enter document-player-box ",
            "mybox enter player": "mybox enter document-player-box ",
            "mybox start doc": "mybox start document-player-box ",
            "mybox copy doc": "mybox copy document-player-box ",
            "mybox ports add doc": "mybox ports add document-player-box ",
            "mybox ports re": "mybox ports remove ",
            "mybox create new --ins": "mybox create new --inside-port ",
            "mybox completion z": "mybox completion zsh ",
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                buffer, output = self.complete(command)
                self.assertEqual(buffer, expected, output)
        self.assertEqual(self.calls(), [["list", "--all", "--format", "json"]] * 5)

    def test_tab_lists_multiple_matches_and_handles_no_matches(self):
        self.save_names("document-player-box", "docs")
        self.start_shell()
        buffer, output = self.complete("mybox enter doc")
        self.assertEqual(buffer, "mybox enter doc", output)
        self.assertIn("document-player-box", output)
        self.assertIn("docs", output)
        buffer, output = self.complete("mybox enter missing")
        self.assertEqual(buffer, "mybox enter missing", output)
        self.assertTrue(all(call == ["list", "--all", "--format", "json"] for call in self.calls()))


if __name__ == "__main__":
    unittest.main()
