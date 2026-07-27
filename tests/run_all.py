"""Run every test suite.

    python tests/run_all.py            # everything
    python tests/run_all.py --no-gui   # skip the tests that open windows
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HEADLESS = ("test_parser.py", "test_receiver.py", "test_sender.py", "test_sync.py",
            "test_logging.py", "test_outbox_config.py", "test_database.py")
GUI = ("test_gui_integration.py",)


def main(argv: list[str]) -> int:
    here = Path(__file__).resolve().parent
    suites = list(HEADLESS)
    if "--no-gui" not in argv:
        suites += list(GUI)

    failures: list[str] = []
    for name in suites:
        print(f"\n=== {name} " + "=" * (60 - len(name)))
        result = subprocess.run([sys.executable, str(here / name)],
                                capture_output=True, text=True)
        tail = [line for line in result.stderr.splitlines()
                if line.startswith(("OK", "FAILED", "Ran ", "ERROR"))]
        print("\n".join(tail) or result.stdout.strip()[-500:])
        if result.returncode != 0:
            failures.append(name)
            print(result.stdout[-2000:])
            print(result.stderr[-4000:])

    print("\n" + "=" * 70)
    if failures:
        print("FAILED:", ", ".join(failures))
        return 1
    print(f"All {len(suites)} suite(s) passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
