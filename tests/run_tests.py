"""runs tests/test_*.py inside the IRIS process (called from tests/run.sh)"""
import sys
import unittest


def main(test_dir):
    # discover() puts test_dir on sys.path, so the tests import each other as plain modules
    suite = unittest.defaultTestLoader.discover(test_dir, pattern="test_*.py")
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(suite)
    return result.wasSuccessful()
