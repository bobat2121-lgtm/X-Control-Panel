"""The master switch (XCP_LIVE): off by default, and while off nothing reaches Postgres, a job or a feed. Offline."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="xcp-test-switch-")
os.environ["DATABASE_URL"] = f"sqlite:///{Path(_TMP, 'test.db').as_posix()}"  # never the real database

from jobs import monitor as monitor_job  # noqa: E402
from jobs import run as run_job  # noqa: E402
from xcp import config, db  # noqa: E402

NEON = "postgresql://user:pw@ep-test.neon.tech/db?sslmode=require"


def _env(**kw):
    """os.environ without XCP_LIVE, plus kw."""
    env = {k: v for k, v in os.environ.items() if k != "XCP_LIVE"}
    return mock.patch.dict(os.environ, {**env, **kw}, clear=True)


class Switch(unittest.TestCase):
    def test_off_unless_set_to_1(self):
        with _env():
            self.assertFalse(config.live())
        for value in ("0", "", "true", "on"):
            with _env(XCP_LIVE=value):
                self.assertFalse(config.live(), value)
        with _env(XCP_LIVE="1"):
            self.assertTrue(config.live())

    def test_off_never_connects_to_postgres(self):
        with _env(DATABASE_URL=NEON), mock.patch.object(db, "_engine", None), \
                mock.patch.object(db, "create_engine") as create:
            with self.assertRaisesRegex(RuntimeError, "switched off"):
                db.engine()
            create.assert_not_called()

    def test_local_sqlite_still_works_while_off(self):
        with _env(), mock.patch.object(db, "_engine", None):
            self.assertTrue(db.engine().url.drivername.startswith("sqlite"))

    def test_jobs_do_nothing_while_off(self):
        with _env(DATABASE_URL=NEON), mock.patch("xcp.scheduler.run_auto") as auto, \
                mock.patch("xcp.agents.monitor.run") as mon, mock.patch.object(db, "session") as session:
            self.assertEqual(run_job.main(["auto"]), 0)
            self.assertEqual(run_job.main(["seed-demo"]), 0)
            self.assertEqual(monitor_job.main([]), 0)
            auto.assert_not_called()
            mon.assert_not_called()
            session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
