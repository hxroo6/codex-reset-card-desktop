import os
import subprocess
import sys
import unittest
import uuid

from schedule_control import named_guard


@unittest.skipUnless(os.name == "nt", "Windows named-mutex integration")
class NativeGuardTests(unittest.TestCase):
    def test_another_process_is_blocked_until_guard_releases(self):
        # A disposable test mutex, never the real daemon or its state.
        name = "Local\\CodexResetCardPanel-Test-" + uuid.uuid4().hex
        child = "from schedule_control import named_guard\nimport sys\ntry:\n with named_guard(sys.argv[1], timeout=0.05): pass\nexcept RuntimeError:\n sys.exit(23)\n"
        with named_guard(name):
            blocked = subprocess.run([sys.executable, "-c", child, name], capture_output=True, timeout=5,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(blocked.returncode, 23, blocked.stderr)
        free = subprocess.run([sys.executable, "-c", child, name], capture_output=True, timeout=5,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(free.returncode, 0, free.stderr)
