#!/usr/bin/env python3
"""Consolidate duplicate AI-Provenance resources within a bundle.

label_bundle.py emits one AI-Provenance resource per AI-labeled resource,
even when several Provenances in the same bundle share an identical
author Device, verifier presence, input prompt, recorded timestamp, and
reason -- differing only in `id` and `target`. This script merges each
such group, within a single bundle, into one Provenance whose `target`
is the union of the group's targets, keeping the first-encountered `id`
and dropping the rest. Provenances are never merged across bundles.

This does not change which targets exist or what device/prompt/verifier
produced them, so test_data/manifest.json's per-device/per-prompt target
sets and counts are unaffected -- only the number of Provenance resources
decreases.

Usage:
    python3 merge_provenance_targets.py --input LABELED.json --output LABELED.json
"""
import argparse
import json
from pathlib import Path


def provenance_group_key(resource):
    """Everything about a Provenance except id and target, as a hashable key."""
    clone = {k: v for k, v in resource.items() if k not in ("id", "target")}
    return json.dumps(clone, sort_keys=True)


def merge_provenances(bundle):
    merged_by_key = {}
    order = []
    new_entries = []
    for entry in bundle["entry"]:
        resource = entry["resource"]
        if resource["resourceType"] != "Provenance":
            new_entries.append(entry)
            continue
        key = provenance_group_key(resource)
        if key not in merged_by_key:
            merged_by_key[key] = entry
            order.append(key)
            new_entries.append(entry)
        else:
            kept = merged_by_key[key]["resource"]
            kept["target"].extend(resource["target"])
    bundle["entry"] = new_entries
    return bundle


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    bundle = json.loads(args.input.read_text())
    before = sum(1 for e in bundle["entry"] if e["resource"]["resourceType"] == "Provenance")
    bundle = merge_provenances(bundle)
    after = sum(1 for e in bundle["entry"] if e["resource"]["resourceType"] == "Provenance")

    args.output.write_text(json.dumps(bundle, indent=2) + "\n")
    print(f"{args.input.name}: {before} -> {after} Provenance resources")


if __name__ == "__main__":
    main()
