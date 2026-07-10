"""One source of truth for the version. A `team --version` that drifts from the
tag is worse than none: it tells a bug reporter the wrong commit."""
import re
import subprocess
import sys
import unittest
from pathlib import Path

import team


class Version(unittest.TestCase):
    def test_is_pep440_ish(self):
        self.assertRegex(team.__version__, r"^\d+\.\d+\.\d+([-.][0-9A-Za-z.]+)?$")

    def test_cli_reports_the_package_version(self):
        root = Path(__file__).resolve().parent.parent
        proc = subprocess.run([sys.executable, "-m", "team", "--version"],
                              capture_output=True, text=True,
                              env={"PYTHONPATH": str(root)})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(team.__version__, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
