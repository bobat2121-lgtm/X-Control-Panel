"""Voice v2: robot check, the human-voice style mode, and the draft → preview → live flow (mock writer)."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="xcp-test-voice-")
os.environ["DATABASE_URL"] = f"sqlite:///{Path(_TMP, 'test.db').as_posix()}"  # never the real database
os.environ["LLM_BACKEND"] = "mock"

from xcp import config, db, voice  # noqa: E402
from xcp.agents import context  # noqa: E402
from xcp.agents.editor import check_variant, robot_flags  # noqa: E402


class RobotCheck(unittest.TestCase):
    def test_flags_what_reads_like_ai(self):
        tidy = ("$SATA held par in 15 of 20 sessions.\n\n$STRC held par in 0 of 20 sessions.\n\n"
                "Same job with a different overhang.")
        self.assertTrue(any("same length and shape" in f for f in robot_flags([tidy])))
        self.assertTrue(any("Em dash" in f for f in robot_flags(["Par is back — finally"])))
        self.assertTrue(any("the tell" in f for f in robot_flags(["That's the tell."])))
        self.assertTrue(any("not X, it's Y" in f for f in robot_flags(["It's not a yield play, it's a BTC play"])))

    def test_leaves_human_posts_alone(self):
        human = ("Woah. $ASST. $30.\n\nAre you kidding me?! $SATA printing again.\n\n"
                 "I actually love this... the engine is humming")
        self.assertEqual(robot_flags([human]), [])

    def test_off_for_live_drafts_until_v2_is_live(self):
        db.engine()
        db.kv_delete("config:settings")  # defaults: classic mode, robot check off
        self.assertFalse(any(f.startswith("🤖") for f in check_variant(["That's the tell."], {}, [])))
        self.assertTrue(any(f.startswith("🤖") for f in check_variant(["That's the tell."], {}, [], robot=True)))


class StyleMode(unittest.TestCase):
    def setUp(self):
        db.engine()
        with db.session() as s:
            s.query(db.StyleExample).delete()
            for i in range(20):
                s.add(db.StyleExample(source="mine", text=f"my post {i}", strength=5))
            for h in ("saylor", "RoaringRagnar", "PunterJeff", "ZynxBTC"):
                s.add(db.StyleExample(source="admired", handle=h, format="long_analysis", pattern=f"{h} pattern",
                                      skeleton="[template]", demo="demo", strength=7))
            s.commit()

    def test_v2_uses_more_of_you_and_no_templates(self):
        classic = context.style_block("btc", seed="x", mode="classic")
        v2 = context.style_block("btc", seed="x", mode="v2")
        self.assertEqual(classic.count("my post"), 10)
        self.assertEqual(v2.count("my post"), 16)
        self.assertIn("Template:", classic)
        self.assertNotIn("Template:", v2)
        self.assertIn("REASONING MOVES", v2)


class DraftFlow(unittest.TestCase):
    def setUp(self):
        db.engine()
        for k in ("config:voice", "config:settings", voice.KV_DRAFT, voice.KV_HISTORY):
            db.kv_delete(k)

    def test_preview_never_touches_live_and_make_live_keeps_history(self):
        config.save("voice", "LIVE v1")
        voice.save_draft("DRAFT v2")
        out = voice.preview("draft")
        self.assertEqual(out["posts"], len(voice.DEFAULT_BRIEFS))
        self.assertEqual(config.get("voice"), "LIVE v1")
        self.assertEqual(db.kv_get("voice:preview:draft")["results"][0]["id"], "P1")
        voice.make_live()
        self.assertEqual(config.get("voice"), "DRAFT v2")
        self.assertEqual(voice.history()[-1]["text"], "LIVE v1")
        self.assertEqual(config.settings()["voice"], {"style_mode": "v2", "robot_check": True})


if __name__ == "__main__":
    unittest.main()
