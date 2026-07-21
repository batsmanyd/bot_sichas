"""Test runner for the approved v2 privacy model.

The legacy test ``test_real_two_user_flow`` asserts that verification selfies are
shown to meeting participants. That behaviour is deliberately prohibited in v2
and is replaced by ``test_selfie_is_never_used_as_meeting_photo``.
"""

import sys
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))


OBSOLETE_TESTS = {
    "test_mvp.MvpFlowTest.test_real_two_user_flow",
}


def filtered(test):
    if isinstance(test, unittest.TestSuite):
        suite = unittest.TestSuite()
        for item in test:
            result = filtered(item)
            if result is not None:
                suite.addTest(result)
        return suite
    return None if test.id() in OBSOLETE_TESTS else test


def main():
    discovered = unittest.defaultTestLoader.discover("tests", pattern="test_*.py")
    suite = filtered(discovered)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
