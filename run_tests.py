"""Run every test with nothing installed:  python -I run_tests.py   (add -v for each test's name)"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    verbosity = 2 if "-v" in argv else 1
    suite = unittest.defaultTestLoader.discover(os.path.join(ROOT, "tests"), top_level_dir=ROOT)
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
