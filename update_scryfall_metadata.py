#!/usr/bin/env python3
import argparse
from app import refresh_scryfall_bulk_metadata

parser=argparse.ArgumentParser(description="Refresh cached Scryfall metadata for printings in inventory.")
parser.add_argument("--force",action="store_true",help="Download even if the current bulk build is already cached.")
args=parser.parse_args()
print(refresh_scryfall_bulk_metadata(force=args.force))
