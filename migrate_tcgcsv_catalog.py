#!/usr/bin/env python3
"""Stage and transactionally import a TCGCSV MTG catalog and price snapshot.

This one-time rehearsal importer is intentionally limited to the isolated
cardscanner_migration_rehearsal database. It does not touch production SQLite
or the existing cardscanner_dev database.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from itertools import islice
from pathlib import Path
from urllib.parse import urlparse

import psycopg
import requests
from psycopg import sql
from psycopg.types.json import Jsonb


GAME = "mtg"
PROVIDER = "tcgcsv"
CATEGORY_ID = "1"
REHEARSAL_DATABASE = "cardscanner_migration_rehearsal"
DEFAULT_DATA_DIR = Path("data")
DEFAULT_SNAPSHOT_DIR = (
    Path.home() / "cardscanner-tcgcsv-migration-source-2026-11-19"
)
DEFAULT_BASE_URL = "https://tcgcsv.com"
DEFAULT_USER_AGENT = "CardScannerMigration/1.0"
DEFAULT_REQUEST_DELAY = 0.25
BATCH_SIZE = 2000
PRICE_FIELDS = (
    "marketPrice",
    "lowPrice",
    "midPrice",
    "highPrice",
    "directLowPrice",
)


def load_json(path: Path, label: str):
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read valid {label}: {path}") from exc
    return value


def write_json(path: Path, value) -> None:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
        handle.write("\n")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_timestamp(value, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Missing timestamp: {label}")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError(f"Invalid timestamp for {label}: {value!r}") from exc
    if parsed.tzinfo is None:
        raise RuntimeError(f"Timestamp has no timezone: {label}")
    return parsed.astimezone(timezone.utc)


def external_id(value, label: str) -> str:
    if value is None or isinstance(value, bool):
        raise RuntimeError(f"Missing or invalid external ID: {label}")
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError) as exc:
        raise RuntimeError(f"Invalid external ID for {label}: {value!r}") from exc
    if not number.is_finite() or number <= 0 or number != number.to_integral_value():
        raise RuntimeError(f"Invalid external ID for {label}: {value!r}")
    return str(int(number))


def decimal_value(value, label: str):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise RuntimeError(f"Invalid numeric price for {label}: {value!r}")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise RuntimeError(f"Invalid numeric price for {label}: {value!r}") from exc
    if not number.is_finite():
        raise RuntimeError(f"Non-finite price for {label}: {value!r}")
    return number


def validate_envelope(
    payload,
    label: str,
    *,
    allow_missing_total_for_empty: bool = False,
):
    if not isinstance(payload, dict):
        raise RuntimeError(f"{label} response is not a JSON object")
    if payload.get("success") is not True:
        raise RuntimeError(f"{label} response was not successful: {payload.get('errors')!r}")
    errors = payload.get("errors", [])
    if errors not in (None, []):
        raise RuntimeError(f"{label} response contains errors: {errors!r}")
    results = payload.get("results")
    if not isinstance(results, list):
        raise RuntimeError(f"{label} response results is not a list")
    total = payload.get("totalItems")
    if total is None and not (
        allow_missing_total_for_empty and not results
    ):
        raise RuntimeError(f"{label} response is missing totalItems")
    if total is not None and int(total) != len(results):
        raise RuntimeError(
            f"{label} totalItems mismatch: {total} != {len(results)}"
        )
    return results


def request_json(session: requests.Session, url: str, *, allow_empty_404: bool = False):
    response = session.get(url, timeout=30)
    if response.status_code == 404 and allow_empty_404:
        try:
            body = response.json()
        except ValueError as exc:
            raise RuntimeError(f"Unparseable 404 response from {url}") from exc

        if not isinstance(body, dict):
            raise RuntimeError(f"Unexpected non-object 404 response from {url}")

        errors = body.get("errors")
        results = body.get("results")
        documented_empty_result = (
            isinstance(errors, list)
            and bool(errors)
            and all(
                isinstance(error, str)
                and "no products found" in error.casefold()
                for error in errors
            )
            and results in (None, [])
            and body.get("success") is not True
        )
        if not documented_empty_result:
            raise RuntimeError(
                f"Unexpected TCGCSV 404 response for {url}: "
                f"errors={errors!r}, results={results!r}"
            )

        # TCGCSV documents an error/404 for a group's empty product collection.
        return {"success": True, "errors": [], "results": [], "totalItems": 0}
    response.raise_for_status()
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(f"Invalid JSON response from {url}") from exc


def validate_price_snapshot(price_snapshot, sync_meta):
    if not isinstance(price_snapshot, dict) or price_snapshot.get("success") is not True:
        raise RuntimeError("tcg_prices.json does not report success=true")
    rows = price_snapshot.get("results")
    if not isinstance(rows, list):
        raise RuntimeError("tcg_prices.json results is not a list")

    source_updated = price_snapshot.get("sourceUpdated")
    if not isinstance(source_updated, str) or not source_updated.strip():
        raise RuntimeError("tcg_prices.json is missing sourceUpdated")
    if sync_meta.get("source_updated") != source_updated:
        raise RuntimeError("price_sync.json and tcg_prices.json source timestamps differ")

    if int(sync_meta.get("category_id", -1)) != int(CATEGORY_ID):
        raise RuntimeError("price_sync.json is not the MTG category 1 snapshot")
    if sync_meta.get("failures") not in (None, []):
        raise RuntimeError("price_sync.json records failed groups")
    groups = sync_meta.get("groups")
    synced_groups = sync_meta.get("groups_synced")
    if not isinstance(groups, int) or groups <= 0 or synced_groups != groups:
        raise RuntimeError("price_sync.json does not report a complete group sync")
    if sync_meta.get("price_rows") != len(rows):
        raise RuntimeError(
            "price_sync.json price_rows does not match tcg_prices.json"
        )

    captured_at = parse_timestamp(sync_meta.get("synced_at"), "price_sync.synced_at")
    source_updated_at = parse_timestamp(source_updated, "sourceUpdated")

    seen = set()
    products = set()
    subtypes = set()
    for index, item in enumerate(rows):
        if not isinstance(item, dict):
            raise RuntimeError(f"Price row {index} is not an object")
        product_id = external_id(item.get("productId"), f"price row {index} productId")
        subtype = item.get("subTypeName")
        if not isinstance(subtype, str) or not subtype.strip():
            raise RuntimeError(f"Price row {index} has no subTypeName")
        key = (product_id, subtype, source_updated)
        if key in seen:
            raise RuntimeError(f"Duplicate price observation key: {key!r}")
        seen.add(key)
        products.add(product_id)
        subtypes.add(subtype)
        for field in PRICE_FIELDS:
            decimal_value(item.get(field), f"price row {index} {field}")

    return {
        "rows": rows,
        "source_updated": source_updated,
        "source_updated_at": source_updated_at,
        "captured_at": captured_at,
        "product_ids": products,
        "subtypes": subtypes,
    }


def validate_groups(payload, expected_category: str):
    groups = validate_envelope(payload, "TCGCSV groups")
    seen = set()
    for index, group in enumerate(groups):
        if not isinstance(group, dict):
            raise RuntimeError(f"Group row {index} is not an object")
        group_id = external_id(group.get("groupId"), f"group row {index} groupId")
        if group_id in seen:
            raise RuntimeError(f"Duplicate TCGCSV group ID: {group_id}")
        seen.add(group_id)
        if external_id(group.get("categoryId"), f"group {group_id} categoryId") != expected_category:
            raise RuntimeError(f"Group {group_id} belongs to the wrong category")
        if not isinstance(group.get("name"), str) or not group["name"].strip():
            raise RuntimeError(f"Group {group_id} has no name")
        if "isSupplemental" in group and not isinstance(group["isSupplemental"], bool):
            raise RuntimeError(f"Group {group_id} has invalid isSupplemental")
    return groups


def validate_products_by_group(products_by_group, groups, expected_category: str):
    group_ids = {
        external_id(group["groupId"], "groupId") for group in groups
    }
    if not isinstance(products_by_group, dict):
        raise RuntimeError("products_by_group.json is not an object")
    if set(products_by_group) != group_ids:
        missing = sorted(group_ids - set(products_by_group))
        extra = sorted(set(products_by_group) - group_ids)
        raise RuntimeError(
            f"Product snapshot group coverage mismatch; missing={missing[:10]}, "
            f"extra={extra[:10]}"
        )

    products = []
    seen_product_ids = set()
    empty_groups = []

    for group_id in sorted(group_ids, key=int):
        payload = products_by_group[group_id]
        group_products = validate_envelope(
            payload,
            f"TCGCSV products for group {group_id}",
            allow_missing_total_for_empty=True,
        )
        if not group_products:
            empty_groups.append(group_id)

        for index, product in enumerate(group_products):
            if not isinstance(product, dict):
                raise RuntimeError(f"Product row {index} in group {group_id} is not an object")
            product_id = external_id(
                product.get("productId"),
                f"group {group_id} product row {index} productId",
            )
            if product_id in seen_product_ids:
                raise RuntimeError(
                    f"Duplicate TCGCSV product ID across groups: {product_id}"
                )
            seen_product_ids.add(product_id)
            if external_id(product.get("categoryId"), f"product {product_id} categoryId") != expected_category:
                raise RuntimeError(f"Product {product_id} belongs to the wrong category")
            if external_id(product.get("groupId"), f"product {product_id} groupId") != group_id:
                raise RuntimeError(f"Product {product_id} is in the wrong group")
            if not isinstance(product.get("name"), str) or not product["name"].strip():
                raise RuntimeError(f"Product {product_id} has no name")
            extended = product.get("extendedData", [])
            if not isinstance(extended, list):
                raise RuntimeError(f"Product {product_id} extendedData is not a list")
            presale = product.get("presaleInfo")
            if presale is not None and not isinstance(presale, dict):
                raise RuntimeError(f"Product {product_id} presaleInfo is not an object")
            image_count = product.get("imageCount")
            if image_count is not None and (
                isinstance(image_count, bool) or not isinstance(image_count, int) or image_count < 0
            ):
                raise RuntimeError(f"Product {product_id} has invalid imageCount")
            products.append(product)

    return products, empty_groups


def stage_snapshot(args):
    destination = args.snapshot_dir.expanduser().resolve()
    source_dir = args.source_data_dir.expanduser().resolve()
    if destination.exists():
        raise RuntimeError(f"Refusing to overwrite existing snapshot directory: {destination}")
    for name in ("tcg_prices.json", "price_sync.json"):
        source = source_dir / name
        if not source.is_file() or source.is_symlink():
            raise FileNotFoundError(f"Missing regular source file: {source}")

    price_snapshot = load_json(source_dir / "tcg_prices.json", "TCGCSV price snapshot")
    sync_meta = load_json(source_dir / "price_sync.json", "TCGCSV price sync metadata")
    validated_prices = validate_price_snapshot(price_snapshot, sync_meta)

    session = requests.Session()
    session.headers.update({
        "User-Agent": args.user_agent,
        "Accept": "application/json,text/plain,*/*",
    })
    base_url = args.base_url.rstrip("/")

    marker_response = session.get(f"{base_url}/last-updated.txt", timeout=30)
    marker_response.raise_for_status()
    live_marker = marker_response.text.strip()
    if live_marker != validated_prices["source_updated"]:
        raise RuntimeError(
            "TCGCSV's current build marker does not match the saved price snapshot: "
            f"{live_marker!r} != {validated_prices['source_updated']!r}. "
            "Do not mix catalog and price builds."
        )

    groups_payload = request_json(
        session, f"{base_url}/tcgplayer/{CATEGORY_ID}/groups"
    )
    groups = validate_groups(groups_payload, CATEGORY_ID)
    if len(groups) != sync_meta["groups"]:
        raise RuntimeError(
            f"Current group count {len(groups)} differs from saved price-sync "
            f"group count {sync_meta['groups']}"
        )

    products_by_group = {}
    for index, group in enumerate(groups, start=1):
        group_id = external_id(group["groupId"], "groupId")
        url = f"{base_url}/tcgplayer/{CATEGORY_ID}/{group_id}/products"
        payload = request_json(session, url, allow_empty_404=True)
        # Validate here so a failed group never becomes a partial snapshot.
        validate_envelope(
            payload,
            f"TCGCSV products for group {group_id}",
            allow_missing_total_for_empty=True,
        )
        products_by_group[group_id] = payload
        if index % 25 == 0 or index == len(groups):
            print(f"Fetched product catalogs for {index}/{len(groups)} groups")
        time.sleep(args.request_delay)

    products, empty_groups = validate_products_by_group(
        products_by_group, groups, CATEGORY_ID
    )
    if len(groups) != sync_meta["groups_synced"]:
        raise RuntimeError("Not all groups from the price snapshot were synchronized")

    stage_parent = destination.parent
    stage_parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(
        prefix=f".{destination.name}.staging-",
        dir=stage_parent,
    ))
    try:
        shutil.copyfile(source_dir / "tcg_prices.json", stage / "tcg_prices.json")
        shutil.copyfile(source_dir / "price_sync.json", stage / "price_sync.json")
        write_json(stage / "groups.json", groups_payload)
        write_json(stage / "products_by_group.json", products_by_group)

        downloaded_at = datetime.now(timezone.utc).isoformat()
        file_hashes = {
            name: sha256_file(stage / name)
            for name in (
                "tcg_prices.json",
                "price_sync.json",
                "groups.json",
                "products_by_group.json",
            )
        }
        manifest = {
            "game_id": GAME,
            "provider_id": PROVIDER,
            "category_id": CATEGORY_ID,
            "source_updated": validated_prices["source_updated"],
            "downloaded_at": downloaded_at,
            "group_count": len(groups),
            "product_count": len(products),
            "price_row_count": len(validated_prices["rows"]),
            "empty_product_group_ids": empty_groups,
            "sha256": file_hashes,
        }
        write_json(stage / "manifest.json", manifest)

        for path in stage.iterdir():
            os.chmod(path, 0o400)
        os.chmod(stage, 0o500)
        os.rename(stage, destination)

    except Exception:
        if stage.exists():
            shutil.rmtree(stage)
        raise

    print("\n=== TCGCSV SNAPSHOT STAGED ===")
    print(f"Snapshot directory: {destination}")
    print(f"Source build: {validated_prices['source_updated']}")
    print(f"Groups: {len(groups):,}")
    print(f"Products: {len(products):,}")
    print(f"Price rows: {len(validated_prices['rows']):,}")
    print(f"Groups with empty product lists (404/empty response): {len(empty_groups):,}")
    print("Files are read-only; source production files were not modified.")


def load_and_validate_snapshot(snapshot_dir: Path):
    if not snapshot_dir.is_dir():
        raise FileNotFoundError(snapshot_dir)
    manifest = load_json(snapshot_dir / "manifest.json", "snapshot manifest")
    if (
        manifest.get("game_id") != GAME
        or manifest.get("provider_id") != PROVIDER
        or str(manifest.get("category_id")) != CATEGORY_ID
    ):
        raise RuntimeError("Snapshot manifest does not identify MTG / TCGCSV category 1")

    expected_files = (
        "tcg_prices.json",
        "price_sync.json",
        "groups.json",
        "products_by_group.json",
    )
    hashes = manifest.get("sha256")
    if not isinstance(hashes, dict):
        raise RuntimeError("Snapshot manifest has no file hashes")
    for name in expected_files:
        path = snapshot_dir / name
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"Snapshot file is missing or not regular: {path}")
        actual = sha256_file(path)
        if hashes.get(name) != actual:
            raise RuntimeError(f"Snapshot SHA256 mismatch: {name}")

    price_snapshot = load_json(snapshot_dir / "tcg_prices.json", "TCGCSV price snapshot")
    sync_meta = load_json(snapshot_dir / "price_sync.json", "TCGCSV price sync metadata")
    groups_payload = load_json(snapshot_dir / "groups.json", "TCGCSV groups snapshot")
    products_by_group = load_json(
        snapshot_dir / "products_by_group.json",
        "TCGCSV products snapshot",
    )

    prices = validate_price_snapshot(price_snapshot, sync_meta)
    groups = validate_groups(groups_payload, CATEGORY_ID)
    products, empty_groups = validate_products_by_group(
        products_by_group, groups, CATEGORY_ID
    )

    if len(groups) != manifest.get("group_count"):
        raise RuntimeError("Group count differs from snapshot manifest")
    if len(products) != manifest.get("product_count"):
        raise RuntimeError("Product count differs from snapshot manifest")
    if len(prices["rows"]) != manifest.get("price_row_count"):
        raise RuntimeError("Price row count differs from snapshot manifest")
    if manifest.get("source_updated") != prices["source_updated"]:
        raise RuntimeError("Manifest source timestamp differs from price snapshot")
    if manifest.get("empty_product_group_ids") != empty_groups:
        raise RuntimeError("Empty product group list differs from snapshot manifest")

    fetched_at = parse_timestamp(manifest.get("downloaded_at"), "manifest.downloaded_at")
    return manifest, groups, products, prices, fetched_at


def batched_rows(rows, size=BATCH_SIZE):
    iterator = iter(rows)
    while True:
        batch = list(islice(iterator, size))
        if not batch:
            return
        yield batch


def insert_batches(pg, statement, rows):
    inserted = 0
    with pg.cursor() as cursor:
        for batch in batched_rows(rows):
            cursor.executemany(statement, batch)
            inserted += len(batch)
    return inserted


def group_records(groups, fetched_at):
    for group in groups:
        group_id = external_id(group["groupId"], "groupId")
        yield (
            GAME,
            PROVIDER,
            CATEGORY_ID,
            group_id,
            group["name"],
            group.get("abbreviation"),
            bool(group.get("isSupplemental", False)),
            group.get("publishedOn"),
            group.get("modifiedOn"),
            Jsonb(group),
            fetched_at,
        )


def product_records(products, fetched_at):
    for product in products:
        presale = product.get("presaleInfo")
        yield (
            GAME,
            PROVIDER,
            external_id(product["productId"], "productId"),
            CATEGORY_ID,
            external_id(product["groupId"], "groupId"),
            product["name"],
            product.get("cleanName"),
            product.get("url"),
            product.get("imageUrl"),
            product.get("imageCount"),
            product.get("modifiedOn"),
            Jsonb(presale) if presale is not None else None,
            Jsonb(product.get("extendedData", [])),
            Jsonb(product),
            fetched_at,
        )


def price_records(prices):
    source_updated = prices["source_updated"]
    captured_at = prices["captured_at"]
    for index, item in enumerate(prices["rows"]):
        product_id = external_id(item["productId"], f"price row {index} productId")
        subtype = item["subTypeName"]
        yield (
            GAME,
            PROVIDER,
            product_id,
            subtype,
            source_updated,
            captured_at,
            decimal_value(item.get("marketPrice"), f"price row {index} marketPrice"),
            decimal_value(item.get("lowPrice"), f"price row {index} lowPrice"),
            decimal_value(item.get("midPrice"), f"price row {index} midPrice"),
            decimal_value(item.get("highPrice"), f"price row {index} highPrice"),
            decimal_value(item.get("directLowPrice"), f"price row {index} directLowPrice"),
            Jsonb(item),
        )


def import_snapshot(args):
    if not args.apply:
        print("Mode: DRY RUN (transaction will roll back)")
    else:
        print("Mode: APPLY (transaction will commit to rehearsal database)")

    dsn = os.environ.get("CARD_SCANNER_POSTGRES_DSN", "").strip()
    if not dsn:
        raise RuntimeError("CARD_SCANNER_POSTGRES_DSN is not set")
    parsed = urlparse(dsn)
    if (
        parsed.hostname, parsed.port, parsed.path.lstrip("/")
    ) != ("127.0.0.1", 5433, args.target_database):
        raise RuntimeError(
            "Refusing to run: DSN must target "
            f"127.0.0.1:5433/{args.target_database}"
        )

    manifest, groups, products, prices, fetched_at = load_and_validate_snapshot(
        args.snapshot_dir.expanduser().resolve()
    )
    product_ids = {external_id(p["productId"], "productId") for p in products}
    unmatched_price_products = prices["product_ids"] - product_ids

    print("=== VALIDATED TCGCSV SOURCE ===")
    print(f"Source build: {prices['source_updated']}")
    print(f"Catalog fetched at: {fetched_at.isoformat()}")
    print(f"Groups: {len(groups):,}")
    print(f"Products: {len(products):,}")
    print(f"Price observations: {len(prices['rows']):,}")
    print(f"Distinct price products: {len(prices['product_ids']):,}")
    print(f"Exact price subtypes: {', '.join(sorted(prices['subtypes']))}")
    print(f"Price products absent from product metadata: {len(unmatched_price_products):,}")
    print("These price-only products are allowed by the PostgreSQL schema.")

    pg = None
    try:
        pg = psycopg.connect(dsn)
        pg.execute("SET LOCAL TIME ZONE 'UTC'")
        pg.execute("SELECT pg_advisory_xact_lock(731942811)")

        database, role = pg.execute(
            "SELECT current_database(), current_user"
        ).fetchone()
        if database != args.target_database:
            raise RuntimeError(f"Unexpected connected database: {database}")

        provider = pg.execute("""
            SELECT provider_category_id, is_enabled
            FROM catalog.game_providers
            WHERE game_id = %s AND provider_id = %s
        """, (GAME, PROVIDER)).fetchone()
        if not provider or provider[0] != CATEGORY_ID or not provider[1]:
            raise RuntimeError(
                "MTG TCGCSV provider is missing, disabled, or has the wrong category"
            )

        print(f"\nPostgreSQL target: {database} as {role}")
        destination_tables = (
            "catalog.provider_product_groups",
            "catalog.provider_products",
            "catalog.provider_price_observations",
        )
        for table in destination_tables:
            count = pg.execute(
                sql.SQL("SELECT COUNT(*) FROM {}").format(
                    sql.Identifier(*table.split("."))
                )
            ).fetchone()[0]
            if count:
                raise RuntimeError(
                    f"Refusing to mix data: {table} already contains {count:,} rows"
                )
        print("All three TCGCSV destination tables are empty.")

        group_statement = """
            INSERT INTO catalog.provider_product_groups (
                game_id, provider_id, external_category_id, external_group_id,
                name, abbreviation, is_supplemental, published_on_source,
                modified_on_source, source_payload, fetched_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        product_statement = """
            INSERT INTO catalog.provider_products (
                game_id, provider_id, external_product_id,
                external_category_id, external_group_id, name, clean_name,
                product_url, image_url, image_count, modified_on_source,
                presale_info, extended_data, source_payload, fetched_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        price_statement = """
            INSERT INTO catalog.provider_price_observations (
                game_id, provider_id, external_product_id, subtype_name,
                source_updated, captured_at, market_price, low_price,
                mid_price, high_price, direct_low_price, source_payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """

        print("\nInserting TCGCSV product groups...")
        group_count = insert_batches(
            pg, group_statement, group_records(groups, fetched_at)
        )
        print(f"  Inserted: {group_count:,}")

        print("Inserting TCGCSV products...")
        product_count = insert_batches(
            pg, product_statement, product_records(products, fetched_at)
        )
        print(f"  Inserted: {product_count:,}")

        print("Inserting exact TCGCSV price observations...")
        observation_count = insert_batches(
            pg, price_statement, price_records(prices)
        )
        print(f"  Inserted: {observation_count:,}")

        wanted = {
            "catalog.provider_product_groups": len(groups),
            "catalog.provider_products": len(products),
            "catalog.provider_price_observations": len(prices["rows"]),
        }
        print("\n=== TCGCSV DESTINATION COUNTS ===")
        for table, expected in wanted.items():
            actual = pg.execute(
                sql.SQL("SELECT COUNT(*) FROM {}").format(
                    sql.Identifier(*table.split("."))
                )
            ).fetchone()[0]
            print(f"{table}: {actual:,} (expected {expected:,})")
            if actual != expected:
                raise RuntimeError(
                    f"Count mismatch in {table}: {actual} != {expected}"
                )

        updated = pg.execute("""
            UPDATE catalog.game_catalog_state
            SET revision = revision + 1, updated_at = now()
            WHERE game_id = %s
        """, (GAME,))
        if updated.rowcount != 1:
            raise RuntimeError("Could not increment MTG catalog revision")

        pg.execute("""
            UPDATE catalog.game_providers
            SET last_attempted_at = %s,
                last_successful_at = %s,
                last_source_updated_at = %s,
                last_status = 'success',
                last_error = NULL,
                last_item_count = %s,
                updated_at = now()
            WHERE game_id = %s AND provider_id = %s
        """, (
            fetched_at,
            fetched_at,
            prices["source_updated_at"],
            observation_count,
            GAME,
            PROVIDER,
        ))

        print(f"\nMTG catalog revision after import: {pg.execute('SELECT revision FROM catalog.game_catalog_state WHERE game_id = %s', (GAME,)).fetchone()[0]}")

        if args.apply:
            pg.commit()
            print("\nTCGCSV MIGRATION COMMITTED.")
        else:
            pg.rollback()
            print("\nDRY RUN COMPLETE: transaction rolled back; no rows committed.")

        print("Source snapshots were read only.")
        print("Production SQLite files were never modified.")

    except Exception:
        if pg is not None:
            pg.rollback()
        raise
    finally:
        if pg is not None:
            pg.close()


def main():
    parser = argparse.ArgumentParser(
        description="Stage or import the MTG TCGCSV catalog and price snapshot."
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help="Fetch TCGCSV groups/products and stage them beside the saved price snapshot.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit the import. Without this flag, all database changes roll back.",
    )
    parser.add_argument(
        "--source-data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help="Directory containing production tcg_prices.json and price_sync.json.",
    )
    parser.add_argument(
        "--snapshot-dir",
        type=Path,
        default=DEFAULT_SNAPSHOT_DIR,
        help="Separate staged, read-only TCGCSV migration snapshot directory.",
    )
    parser.add_argument(
        "--target-database",
        choices=(REHEARSAL_DATABASE,),
        default=REHEARSAL_DATABASE,
        help="The only database this one-time importer will accept.",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--request-delay", type=float, default=DEFAULT_REQUEST_DELAY)
    args = parser.parse_args()

    if args.request_delay < 0.1:
        raise RuntimeError("TCGCSV request delay must be at least 0.1 seconds")
    if args.download and args.apply:
        raise RuntimeError("--download and --apply cannot be used together")

    if args.download:
        stage_snapshot(args)
    else:
        import_snapshot(args)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"\nTCGCSV MIGRATION STOPPED: {exc}", file=sys.stderr)
        sys.exit(1)
