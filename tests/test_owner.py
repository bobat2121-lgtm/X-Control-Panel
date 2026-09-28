"""Owner unlock remembered for 30 days: the signed browser token, and the page handing it back. Offline."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="xcp-test-owner-")
os.environ["DATABASE_URL"] = f"sqlite:///{Path(_TMP, 'test.db').as_posix()}"  # never the real database

from panel import common  # noqa: E402

NOW = 1_790_000_000.0


class OwnerToken(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(os.environ, {"PANEL_PASSWORD": "test-only-password"})
        p.start()
        self.addCleanup(p.stop)

    def test_a_fresh_token_is_good_for_30_days(self):
        t = common.owner_token(NOW)
        self.assertTrue(common.token_ok(t, NOW))
        self.assertTrue(common.token_ok(t, NOW + 29 * 86400))
        self.assertFalse(common.token_ok(t, NOW + 30 * 86400 + 1))

    def test_tampering_fails(self):
        exp, _, sig = common.owner_token(NOW).partition(".")
        self.assertFalse(common.token_ok(f"{int(exp) + 86400 * 365}.{sig}", NOW))  # pushed the expiry out
        self.assertFalse(common.token_ok(f"{exp}.{'0' * 64}", NOW))
        for junk in (None, "", "abc", "123", f"{exp}.", ".sig"):
            self.assertFalse(common.token_ok(junk, NOW))

    def test_changing_the_password_signs_everyone_out(self):
        t = common.owner_token(NOW)
        with mock.patch.dict(os.environ, {"PANEL_PASSWORD": "a-new-password"}):
            self.assertFalse(common.token_ok(t, NOW))


class RememberedBrowser(unittest.TestCase):
    """The page sends the pass it saved; the server unlocks only for a valid one, and never after 🔒 Lock."""

    def setUp(self):
        for p in (mock.patch.dict(os.environ, {"PANEL_PASSWORD": "test-only-password"}),
                  mock.patch.object(common.st, "session_state", {})):
            p.start()
            self.addCleanup(p.stop)

    def _page_sends(self, token):
        with mock.patch.object(common, "_owner_reader", lambda **kw: {"token": token}):
            common.recall_owner()

    def test_a_saved_pass_unlocks_after_a_reload(self):
        self.assertFalse(common.is_owner())
        self._page_sends(common.owner_token())
        self.assertTrue(common.is_owner())

    def test_a_forged_or_expired_pass_does_not(self):
        exp, _, sig = common.owner_token().partition(".")
        for bad in (None, "", f"{exp}.{'0' * 64}", common.owner_token(NOW - 40 * 86400)):
            self._page_sends(bad)
            self.assertFalse(common.is_owner(), bad)

    def test_lock_wins_over_a_pass_sent_later(self):
        self._page_sends(common.owner_token())
        common._lock()
        self._page_sends(common.owner_token())
        self.assertFalse(common.is_owner())
        self.assertEqual(common.st.session_state["_owner_cookie"], "")  # the browser forgets it too


if __name__ == "__main__":
    unittest.main()
