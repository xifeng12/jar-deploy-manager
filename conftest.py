import os
import sys
import tempfile


_previous_home = os.environ.get("DEPLOY_HOME")
_test_home = tempfile.TemporaryDirectory(prefix="jar-deploy-tests-")
os.environ["DEPLOY_HOME"] = _test_home.name


def pytest_unconfigure(config):
    database = sys.modules.get("db")
    if database is not None and database._conn is not None:
        database._conn.close()
        database._conn = None
    if _previous_home is None:
        os.environ.pop("DEPLOY_HOME", None)
    else:
        os.environ["DEPLOY_HOME"] = _previous_home
    _test_home.cleanup()
