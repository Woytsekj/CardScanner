"""Regression tests for exact-printing deck price summaries."""
from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cardscanner-deck-pricing-import-"))
os.environ.setdefault("REQUIRE_LOGIN", "1")
os.environ.setdefault("SESSION_SECRET", "unit-test-session-secret-0123456789-this-is-not-production")
os.environ.setdefault("SCANNER_API_KEY", "unit-test-scanner-api-key-0123456789-not-production")
os.environ.setdefault("ADMIN_USERNAME", "ci-admin")
os.environ.setdefault("ADMIN_PASSWORD", "unit-test-admin-password-0123456789")
os.environ.setdefault("TRUSTED_HOSTS", "localhost,127.0.0.1")
os.environ.setdefault("LOCAL_HOSTS", "localhost,127.0.0.1")
os.environ.setdefault("PUBLIC_HOSTNAME", "localhost")
os.environ.setdefault("PUBLIC_HTTPS", "0")

import app as app_module  # noqa: E402


class DeckPricingTests(unittest.TestCase):
    def test_summary_uses_low_and_market_prices_for_exact_finish(self) -> None:
        comparison = [
            {"tcgplayer_id": "100", "finish": "normal", "required": 2, "owned": 1, "missing": 1},
            {"tcgplayer_id": "200", "finish": "foil", "required": 1, "owned": 1, "missing": 0},
        ]
        prices = {
            "100": {"normal": 4.25, "normal_low": 3.50, "foil": 9.00, "foil_low": 8.00},
            "200": {"normal": 2.00, "normal_low": 1.50, "foil": 7.50, "foil_low": 6.00},
        }

        summary = app_module.deck_price_summary(comparison, prices)

        self.assertEqual(summary["low_total"], "$13.00")
        self.assertEqual(summary["market_total"], "$16.00")
        self.assertEqual(summary["owned_value"], "$11.75")
        self.assertEqual(summary["missing_cost"], "$4.25")

    def test_unavailable_low_price_does_not_hide_market_price(self) -> None:
        summary = app_module.deck_price_summary(
            [{"tcgplayer_id": "300", "finish": "normal", "required": 1, "owned": 1, "missing": 0}],
            {"300": {"normal": 5.00, "normal_low": None}},
        )

        self.assertEqual(summary["low_total"], "—")
        self.assertEqual(summary["market_total"], "$5.00")
        self.assertEqual(summary["owned_value"], "$5.00")
        self.assertEqual(summary["missing_cost"], "$0.00")
        self.assertIn("1 copy", summary["low_detail"])

    def test_partial_total_keeps_priced_cards_when_another_copy_has_no_price(self) -> None:
        summary = app_module.deck_price_summary(
            [
                {"tcgplayer_id": "400", "finish": "normal", "required": 1, "owned": 1, "missing": 0},
                {"tcgplayer_id": None, "finish": "normal", "required": 1, "owned": 0, "missing": 1},
            ],
            {"400": {"normal": 5.00, "normal_low": 4.00}},
        )
        self.assertEqual(summary["low_total"], "$4.00")
        self.assertEqual(summary["market_total"], "$5.00")
        self.assertIn("1 copy", summary["market_detail"])

    def test_zero_dollar_priced_card_still_shows_partial_total(self) -> None:
        summary = app_module.deck_price_summary(
            [
                {"tcgplayer_id": "401", "finish": "normal", "required": 1, "owned": 1, "missing": 0},
                {"tcgplayer_id": None, "finish": "normal", "required": 1, "owned": 0, "missing": 1},
            ],
            {"401": {"normal": 0.00, "normal_low": 0.00}},
        )
        self.assertEqual(summary["market_total"], "$0.00")
        self.assertIn("Partial total", summary["market_detail"])

    def test_cards_without_tcgplayer_prices_are_not_undercounted(self) -> None:
        summary = app_module.deck_price_summary(
            [{"tcgplayer_id": None, "finish": "normal", "required": 2, "owned": 1, "missing": 1}],
            {},
        )

        self.assertEqual(summary["low_total"], "—")
        self.assertEqual(summary["market_total"], "—")
        self.assertEqual(summary["owned_value"], "—")
        self.assertEqual(summary["missing_cost"], "—")
        self.assertIn("2 copies", summary["low_detail"])


if __name__ == "__main__":
    unittest.main()
