"""Regression checks for the shared UI shell and issue #28 layout cleanup."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class Issue28UIFoundationTests(unittest.TestCase):
    def read(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_main_pages_use_the_same_header_partial(self):
        for name in ("index.html", "decks.html", "deck_view.html", "vending.html",
                     "admin.html", "analytics.html", "preferences.html", "search_guide.html"):
            with self.subTest(template=name):
                self.assertIn("{% include '_app_header.html' %}", self.read(f"templates/{name}"))

    def test_header_has_consistent_navigation_and_active_page_support(self):
        header = self.read("templates/_app_header.html")
        for endpoint in ("index", "decks_page", "vending_view", "analytics_page",
                         "search_guide", "preferences_page", "admin_page"):
            with self.subTest(endpoint=endpoint):
                self.assertIn(f"url_for('{endpoint}')", header)
        self.assertIn('aria-current="page"', header)
        self.assertIn("url_for('logout')", header)

    def test_theme_selector_is_only_rendered_on_preferences_page(self):
        script = self.read("static/theme.js")
        self.assertIn("data-theme-select", script)
        self.assertNotIn("querySelector('.topbar')", script)
        self.assertNotIn("insertBefore", script)
        for name in ("index.html", "decks.html", "deck_view.html", "vending.html", "admin.html"):
            with self.subTest(template=name):
                self.assertNotIn("data-theme-select", self.read(f"templates/{name}"))
        self.assertIn("data-theme-select", self.read("templates/preferences.html"))

    def test_inventory_summary_values_move_to_analytics(self):
        source = self.read("templates/index.html")
        self.assertNotIn("mobile-summary-menu", source)
        self.assertNotIn("owner_totals.items()", source)
        self.assertNotIn("Collection analytics", source)
        self.assertIn("url_for('analytics_page')", self.read("templates/_app_header.html"))

    def test_inventory_owns_the_manual_add_workflow(self):
        inventory = self.read("templates/index.html")
        admin = self.read("templates/admin.html")
        self.assertIn('id="openManualAdd"', inventory)
        self.assertIn('id="manualAddDialog"', inventory)
        self.assertIn("url_for('manual_add')", inventory)
        self.assertNotIn("Add Card Manually", admin)
        self.assertIn("Catalog search aliases", admin)

    def test_deck_import_form_is_collapsed_above_full_width_deck_list(self):
        source = self.read("templates/decks.html")
        self.assertIn('<details class="deck-import-disclosure admin-panel">', source)
        self.assertIn('<section class="admin-panel stored-decks-panel">', source)
        self.assertIn("deck-dashboard-grid", source)

    def test_search_guide_documents_supported_local_operators(self):
        guide = self.read("templates/search_guide.html")
        for operator in ("set:", "cn:", "name:", "t:", "o:", "c:g", "r&gt;=", "is:foil",
                         "owner:", "loc:", "deck:", "vending:", "art:", "otag:",
                         "order:", "direction:"):
            with self.subTest(operator=operator):
                self.assertIn(operator, guide)
        self.assertIn("not the full Scryfall search engine", guide)

    def test_search_guide_examples_are_generic(self):
        guide = self.read("templates/search_guide.html")
        self.assertIn("Sample Collector", guide)
        self.assertIn("Sample Deck", guide)

    def test_analytics_only_renders_the_signed_in_users_personal_total(self):
        route = self.read("app.py")
        template = self.read("templates/analytics.html")
        self.assertIn("owner_totals.get(person_name)", route)
        self.assertNotIn("owner_totals.items()", template)
        self.assertIn("Historical snapshots are not available yet", template)


if __name__ == "__main__":
    unittest.main()
