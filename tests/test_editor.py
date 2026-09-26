"""Editor rules that follow the owner's voice decisions (Sep 26, 2026)."""
from __future__ import annotations

import unittest

from xcp.agents.editor import check_variant

KNOWN = ["$MSTR 158.61", "BTC 84,000", "846,000 BTC", "bear 20% base 32% bull 40% 6.2x 16x 29x"]


def flags(text: str) -> list[str]:
    return check_variant([text], {}, KNOWN)


class VoiceRules(unittest.TestCase):
    def test_position_sizes_must_be_xx(self):
        for text in ("My portfolio is 84% $MSTR. LONG", "I sold 5% of my stack into this rip.",
                     "My average entry on $MSTR is $117."):
            self.assertTrue(any("write XX" in f for f in flags(text)), text)
        for text in ("My portfolio is XX% $MSTR. LONG", "Strategy bought 846,000 BTC worth of conviction.",
                     "I sold my car in February 2026 for BTC, MSTR and ASST.",
                     "My best guess: another big week for $STRC."):
            self.assertFalse(any("write XX" in f for f in flags(text)), text)

    def test_to_the_moon_is_allowed_now(self):
        self.assertFalse(any("to the moon" in f for f in flags("$STRC to the moon")))

    def test_single_price_targets_still_flagged_but_scenarios_are_fine(self):
        self.assertTrue(any("price target" in f for f in flags("My $MSTR price target is 500")))
        scenario = "10-yr $MSTR CAGR: bear 20% (6.2x), base 32% (16x), bull 40% (29x)."
        self.assertEqual([f for f in flags(scenario) if "Avoid" in f or "XX" in f], [])

    def test_hashtags_still_flagged(self):
        self.assertIn("Contains hashtags", flags("Big week for #Bitcoin"))


if __name__ == "__main__":
    unittest.main()
