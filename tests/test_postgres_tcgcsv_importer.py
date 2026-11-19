"""Unit tests for the transactional TCGCSV migration rehearsal importer."""

from __future__ import annotations

import unittest
from unittest.mock import Mock

from migrate_tcgcsv_catalog import (
    decimal_value,
    external_id,
    group_records,
    parse_timestamp,
    price_records,
    product_records,
    request_json,
    validate_groups,
    validate_price_snapshot,
    validate_products_by_group,
)


class TCGCSVIdentifierTests(unittest.TestCase):
    def test_external_ids_are_canonical_decimal_integers(self):
        self.assertEqual(external_id(123, "id"), "123")
        self.assertEqual(external_id(123.0, "id"), "123")
        self.assertEqual(external_id("00123", "id"), "123")

    def test_external_ids_reject_nonpositive_fractional_and_boolean_values(self):
        for value in (None, True, False, 0, -1, 1.5, "not-an-id", "NaN"):
            with self.subTest(value=value):
                with self.assertRaises(RuntimeError):
                    external_id(value, "test id")

    def test_prices_preserve_null_and_decimal_values(self):
        self.assertIsNone(decimal_value(None, "price"))
        self.assertIsNone(decimal_value("", "price"))
        self.assertEqual(str(decimal_value("1.2500", "price")), "1.2500")

        for value in (True, "NaN", "Infinity", "not-a-price"):
            with self.subTest(value=value):
                with self.assertRaises(RuntimeError):
                    decimal_value(value, "price")

    def test_source_timestamps_must_include_timezone(self):
        self.assertEqual(
            parse_timestamp("2026-11-18T20:05:56+0000", "source"),
            parse_timestamp("2026-11-18T20:05:56Z", "source"),
        )
        with self.assertRaisesRegex(RuntimeError, "no timezone"):
            parse_timestamp("2026-11-18T20:05:56", "source")


class TCGCSVHTTPTests(unittest.TestCase):
    def test_documented_empty_product_404_is_normalized_to_empty_envelope(self):
        response = Mock()
        response.status_code = 404
        response.json.return_value = {
            "success": False,
            "errors": ["No products found for this group."],
            "results": None,
        }
        session = Mock()
        session.get.return_value = response

        payload = request_json(
            session,
            "https://tcgcsv.invalid/tcgplayer/1/123/products",
            allow_empty_404=True,
        )

        self.assertEqual(
            payload,
            {
                "success": True,
                "errors": [],
                "results": [],
                "totalItems": 0,
            },
        )
        session.get.assert_called_once()

    def test_unrelated_404_is_not_mistaken_for_an_empty_group(self):
        response = Mock()
        response.status_code = 404
        response.json.return_value = {
            "success": False,
            "errors": ["Category not found"],
            "results": None,
        }
        session = Mock()
        session.get.return_value = response

        with self.assertRaisesRegex(RuntimeError, "Unexpected TCGCSV 404"):
            request_json(
                session,
                "https://tcgcsv.invalid/tcgplayer/999/123/products",
                allow_empty_404=True,
            )

    def test_successful_json_envelope_is_returned_for_validation(self):
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "success": True,
            "errors": [],
            "totalItems": 0,
            "results": [],
        }
        session = Mock()
        session.get.return_value = response

        self.assertEqual(
            request_json(session, "https://tcgcsv.invalid/groups")["results"],
            [],
        )


class TCGCSVPriceSnapshotTests(unittest.TestCase):
    def make_snapshot(self, rows=None):
        rows = rows if rows is not None else [
            {
                "productId": 123,
                "subTypeName": "Normal",
                "marketPrice": 1.25,
                "lowPrice": 1.00,
                "midPrice": 1.10,
                "highPrice": 1.50,
                "directLowPrice": None,
            },
            {
                "productId": 123,
                "subTypeName": "Foil",
                "marketPrice": 4.50,
                "lowPrice": 4.00,
                "midPrice": 4.25,
                "highPrice": 5.00,
                "directLowPrice": 3.75,
            },
        ]
        return (
            {"success": True, "sourceUpdated": "2026-11-18T20:05:56+0000", "results": rows},
            {
                "category_id": 1,
                "source_updated": "2026-11-18T20:05:56+0000",
                "synced_at": "2026-11-18T20:56:46.604871+00:00",
                "groups": 459,
                "groups_synced": 459,
                "price_rows": len(rows),
                "failures": [],
            },
        )

    def test_snapshot_preserves_exact_subtypes_and_source_build(self):
        snapshot, meta = self.make_snapshot()
        validated = validate_price_snapshot(snapshot, meta)

        self.assertEqual(validated["source_updated"], "2026-11-18T20:05:56+0000")
        self.assertEqual(validated["product_ids"], {"123"})
        self.assertEqual(validated["subtypes"], {"Normal", "Foil"})
        self.assertEqual(len(validated["rows"]), 2)
        records = list(price_records(validated))
        self.assertEqual(records[0][2:5], ("123", "Normal", "2026-11-18T20:05:56+0000"))
        self.assertEqual(records[0][6], decimal_value("1.25", "test"))
        self.assertIsNone(records[0][10])
        self.assertEqual(records[0][11].obj["subTypeName"], "Normal")
        self.assertEqual(records[1][3], "Foil")

    def test_duplicate_observation_keys_are_rejected(self):
        snapshot, meta = self.make_snapshot()
        snapshot["results"][1]["subTypeName"] = "Normal"
        with self.assertRaisesRegex(RuntimeError, "Duplicate price observation"):
            validate_price_snapshot(snapshot, meta)

    def test_snapshot_rejects_partial_group_sync_and_timestamp_mismatch(self):
        snapshot, meta = self.make_snapshot()
        meta["groups_synced"] = 458
        with self.assertRaisesRegex(RuntimeError, "complete group sync"):
            validate_price_snapshot(snapshot, meta)

        snapshot, meta = self.make_snapshot()
        meta["source_updated"] = "2026-11-17T20:05:56+0000"
        with self.assertRaisesRegex(RuntimeError, "timestamps differ"):
            validate_price_snapshot(snapshot, meta)

    def test_snapshot_rejects_invalid_price_values(self):
        snapshot, meta = self.make_snapshot()
        snapshot["results"][0]["marketPrice"] = "NaN"
        with self.assertRaisesRegex(RuntimeError, "Non-finite price"):
            validate_price_snapshot(snapshot, meta)


class TCGCSVCatalogSnapshotTests(unittest.TestCase):
    def make_groups(self):
        return {
            "success": True,
            "errors": [],
            "totalItems": 2,
            "results": [
                {
                    "groupId": 10,
                    "categoryId": 1,
                    "name": "Test Set",
                    "abbreviation": "TST",
                    "isSupplemental": False,
                    "publishedOn": "2026-01-01T00:00:00",
                },
                {
                    "groupId": 11,
                    "categoryId": 1,
                    "name": "Empty Group",
                    "isSupplemental": True,
                },
            ],
        }

    def make_products_by_group(self):
        return {
            "10": {
                "success": True,
                "errors": [],
                "totalItems": 1,
                "results": [
                    {
                        "productId": 900,
                        "categoryId": 1,
                        "groupId": 10,
                        "name": "Test Card",
                        "cleanName": "Test Card",
                        "url": "https://example.invalid/product/900",
                        "imageUrl": "https://example.invalid/image.jpg",
                        "imageCount": 1,
                        "presaleInfo": {"isPresale": False},
                        "extendedData": [{"name": "Rarity", "value": "R"}],
                    }
                ],
            },
            "11": {
                "success": True,
                "errors": [],
                "totalItems": 0,
                "results": [],
            },
        }

    def test_groups_products_and_empty_groups_are_validated(self):
        groups_payload = self.make_groups()
        groups = validate_groups(groups_payload, "1")
        products, empty = validate_products_by_group(
            self.make_products_by_group(),
            groups,
            "1",
        )

        self.assertEqual(len(groups), 2)
        self.assertEqual(len(products), 1)
        self.assertEqual(empty, ["11"])

        group = next(group_records(groups, parse_timestamp(
            "2026-11-19T12:00:00+00:00", "fetched"
        )))
        self.assertEqual(group[3], "10")
        self.assertEqual(group[9].obj["groupId"], 10)

        product = next(product_records(
            products,
            parse_timestamp("2026-11-19T12:00:00+00:00", "fetched"),
        ))
        self.assertEqual(product[2], "900")
        self.assertEqual(product[12].obj[0]["name"], "Rarity")
        self.assertEqual(product[13].obj["productId"], 900)

    def test_empty_product_group_may_omit_total_items(self):
        groups = validate_groups(self.make_groups(), "1")
        products = self.make_products_by_group()
        del products["11"]["totalItems"]

        validated, empty = validate_products_by_group(
            products,
            groups,
            "1",
        )

        self.assertEqual(len(validated), 1)
        self.assertEqual(empty, ["11"])

    def test_nonempty_product_group_must_include_total_items(self):
        groups = validate_groups(self.make_groups(), "1")
        products = self.make_products_by_group()
        del products["10"]["totalItems"]

        with self.assertRaisesRegex(RuntimeError, "missing totalItems"):
            validate_products_by_group(products, groups, "1")

    def test_group_catalog_must_include_total_items(self):
        groups_payload = self.make_groups()
        del groups_payload["totalItems"]

        with self.assertRaisesRegex(RuntimeError, "missing totalItems"):
            validate_groups(groups_payload, "1")

    def test_products_must_cover_every_group(self):
        groups = validate_groups(self.make_groups(), "1")
        products = self.make_products_by_group()
        del products["11"]

        with self.assertRaisesRegex(RuntimeError, "coverage mismatch"):
            validate_products_by_group(products, groups, "1")

    def test_products_cannot_be_duplicated_across_groups(self):
        groups = validate_groups(self.make_groups(), "1")
        products = self.make_products_by_group()
        duplicate = dict(products["10"]["results"][0])
        duplicate["groupId"] = 11
        products["11"] = {
            "success": True,
            "errors": [],
            "totalItems": 1,
            "results": [duplicate],
        }

        with self.assertRaisesRegex(RuntimeError, "Duplicate TCGCSV product ID"):
            validate_products_by_group(products, groups, "1")

    def test_product_in_wrong_category_or_group_is_rejected(self):
        groups = validate_groups(self.make_groups(), "1")
        products = self.make_products_by_group()
        products["10"]["results"][0]["categoryId"] = 3

        with self.assertRaisesRegex(RuntimeError, "wrong category"):
            validate_products_by_group(products, groups, "1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
