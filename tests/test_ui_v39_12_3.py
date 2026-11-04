from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class UIRelease39123Tests(unittest.TestCase):
    def read(self, relative):
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_inventory_defaults_to_grid(self):
        self.assertIn('view = request.args.get("view", "grid")', self.read("app.py"))

    def test_grid_price_order_is_scanned_normal_then_row_foil(self):
        source = self.read("templates/index.html")
        price_grid = source.split('<div class="price-grid">', 1)[1].split('</div>\n          <div class="tile-controls">', 1)[0]
        labels = ["price-scanned", "price-normal", "price-row", "price-foil"]
        positions = [price_grid.index(label) for label in labels]
        self.assertEqual(positions, sorted(positions))

    def test_inventory_has_pagination_above_and_below_results(self):
        source = self.read("templates/index.html")
        self.assertEqual(source.count('class="inventory-pagination'), 2)

    def test_theme_picker_is_loaded_on_main_app_pages(self):
        for name in ("index.html", "vending.html", "decks.html", "deck_view.html", "admin.html"):
            with self.subTest(template=name):
                self.assertIn("filename='theme.js', v='39.12.4'", self.read(f"templates/{name}"))
        script = self.read("static/theme.js")
        for theme in ("dark", "midnight", "forest", "light"):
            self.assertIn(f"id: '{theme}'", script)

    def test_login_page_does_not_load_theme_picker(self):
        login = self.read("templates/login.html")
        self.assertNotIn("theme.js", login)
        self.assertNotIn("theme-picker", login)
        self.assertNotIn("authCard", self.read("static/theme.js"))

    def test_vending_has_history_action_and_dialog(self):
        source = self.read("templates/vending.html")
        self.assertIn('class="button secondary price-history-button"', source)
        self.assertIn('id="priceHistoryDialog"', source)
        self.assertIn("filename='price-history.js', v='39.12.4'", source)

    def test_vendor_history_api_is_scoped_to_vending_inventory(self):
        source = self.read("app.py")
        self.assertIn('@app.get("/api/price-history/<tcg_id>")\n@vendor_or_admin_required', source)
        self.assertIn('COALESCE(i.for_vending, 0) = 1', source)


if __name__ == "__main__":
    unittest.main()
