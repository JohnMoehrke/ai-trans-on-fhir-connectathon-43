#!/usr/bin/env python3
"""Trim a Synthea R4 transaction bundle down to a small, self-consistent bundle.

Keeps the Patient plus a handful of Encounters and the clinical resources
(Condition, Observation, Procedure, MedicationRequest, Immunization,
AllergyIntolerance) tied to those encounters (or, for AllergyIntolerance,
tied to the patient directly since it has no encounter field). Drops bulk
resource types (Claim, ExplanationOfBenefit, Synthea's own Provenance,
CareTeam, CarePlan, DiagnosticReport, DocumentReference, Device,
SupplyDelivery, ImagingStudy) and strips/repairs any reference that would
otherwise dangle, without ever altering a clinical code or value.

Also converts every kept entry from Synthea's POST + urn:uuid style to
PUT with a client-assigned id, per the project's ID rules: resource.id
becomes the uuid from the fullUrl, request.method becomes PUT,
request.url becomes "<ResourceType>/<uuid>", and every internal
"urn:uuid:<x>" reference is rewritten to "<ResourceType>/<x>".

Usage:
    python3 trim_bundle.py --input SYNTHEA_BUNDLE.json --output TRIMMED.json \\
        --seed 1 [--max-resources 35] [--min-resources 15] [--max-encounters 5]
"""
import argparse
import json
import random
import sys
from pathlib import Path

KEEP_TYPES = {
    "Encounter",
    "Condition",
    "Observation",
    "Procedure",
    "MedicationRequest",
    "Immunization",
    "AllergyIntolerance",
}

# Fields that only ever point at resources we never keep (Practitioner,
# Organization, Location are referenced by Synthea via conditional search
# references like "Practitioner?identifier=...", never included as bundle
# entries) or that are simply optional provenance-of-care metadata. Always
# safe to drop outright.
ALWAYS_STRIP_FIELDS = {
    "Encounter": ["participant", "location", "serviceProvider"],
    "Procedure": ["location", "performer"],
    "Immunization": ["location", "performer"],
    "MedicationRequest": ["requester"],
    "Condition": ["asserter", "recorder"],
    "AllergyIntolerance": ["asserter", "recorder"],
    "Observation": ["performer"],
}

# Fields that are lists of {"reference": ...} and must be filtered down to
# entries that still resolve within the kept set (rather than stripped
# unconditionally), because they sometimes point at a kept Condition.
REFERENCE_LIST_FIELDS = {
    "Procedure": ["reasonReference"],
    "MedicationRequest": ["reasonReference"],
}

# Fields that are single {"reference": ...} refs to another clinical
# resource in this bundle (not Patient) that must be dropped if the
# target isn't kept.
ENCOUNTER_FIELD_BY_TYPE = {
    "Condition": "encounter",
    "Observation": "encounter",
    "Procedure": "encounter",
    "MedicationRequest": "encounter",
    "Immunization": "encounter",
    # AllergyIntolerance has no encounter field in R4.
}


def uuid_from_full_url(full_url: str) -> str:
    assert full_url.startswith("urn:uuid:"), f"unexpected fullUrl: {full_url}"
    return full_url[len("urn:uuid:"):]


def ref_uuid(ref_obj):
    if not isinstance(ref_obj, dict):
        return None
    ref = ref_obj.get("reference")
    if not isinstance(ref, str) or not ref.startswith("urn:uuid:"):
        return None
    return ref[len("urn:uuid:"):]


def select_bundle(entries, seed, max_resources, min_resources, max_encounters):
    by_url = {e["fullUrl"]: e for e in entries}
    url_type = {e["fullUrl"]: e["resource"]["resourceType"] for e in entries}

    patients = [e for e in entries if e["resource"]["resourceType"] == "Patient"]
    if len(patients) != 1:
        raise ValueError(f"expected exactly 1 Patient, found {len(patients)}")
    patient_entry = patients[0]

    encounters_all = [e for e in entries if e["resource"]["resourceType"] == "Encounter"]
    clinical_by_type = {t: [] for t in KEEP_TYPES if t != "Encounter"}
    for e in entries:
        rt = e["resource"]["resourceType"]
        if rt in clinical_by_type:
            clinical_by_type[rt].append(e)

    rng = random.Random(seed)

    for encounter_budget in [max_encounters, max_encounters * 2, max_encounters * 3, len(encounters_all)]:
        encounter_budget = min(encounter_budget, len(encounters_all))
        chosen_encounters = rng.sample(encounters_all, k=encounter_budget) if encounters_all else []
        kept_encounter_urls = {e["fullUrl"] for e in chosen_encounters}

        eligible_by_type = {}
        for rt, resources in clinical_by_type.items():
            enc_field = ENCOUNTER_FIELD_BY_TYPE.get(rt)
            eligible = []
            for res_entry in resources:
                res = res_entry["resource"]
                if enc_field is None:
                    eligible.append(res_entry)
                    continue
                enc_ref = res.get(enc_field)
                if enc_ref is None:
                    # No encounter tie (rare) -- keep it, it can only
                    # reference the patient.
                    eligible.append(res_entry)
                    continue
                enc_url = "urn:uuid:" + ref_uuid(enc_ref) if ref_uuid(enc_ref) else None
                if enc_url in kept_encounter_urls:
                    eligible.append(res_entry)
            eligible_by_type[rt] = eligible

        total = 1 + len(chosen_encounters) + sum(len(v) for v in eligible_by_type.values())
        if total >= min_resources or encounter_budget == len(encounters_all):
            break

    # Trim down to max_resources if needed, round-robin across types so no
    # single type crowds out the others, but always keep at least one of
    # each type that has any candidates (useful variety for labeling).
    selected_by_type = {rt: [] for rt in eligible_by_type}
    remaining = {rt: list(v) for rt, v in eligible_by_type.items()}
    for rt, pool in remaining.items():
        rng.shuffle(pool)

    budget = max_resources - 1 - len(chosen_encounters)
    # First pass: guarantee one of each non-empty type.
    for rt, pool in remaining.items():
        if pool and budget > 0:
            selected_by_type[rt].append(pool.pop())
            budget -= 1
    # Round-robin fill remaining budget.
    progressed = True
    while budget > 0 and progressed:
        progressed = False
        for rt, pool in remaining.items():
            if budget <= 0:
                break
            if pool:
                selected_by_type[rt].append(pool.pop())
                budget -= 1
                progressed = True

    kept_entries = [patient_entry] + chosen_encounters
    for rt in selected_by_type:
        kept_entries.extend(selected_by_type[rt])

    kept_urls = {e["fullUrl"] for e in kept_entries}
    return kept_entries, kept_urls, url_type, by_url


def repair_references(entry, kept_urls):
    resource = entry["resource"]
    rt = resource["resourceType"]

    for field in ALWAYS_STRIP_FIELDS.get(rt, []):
        resource.pop(field, None)

    for field in REFERENCE_LIST_FIELDS.get(rt, []):
        items = resource.get(field)
        if not isinstance(items, list):
            continue
        kept_items = []
        for item in items:
            uid = ref_uuid(item)
            if uid is None:
                # Not an internal reference we understand -- drop it to
                # avoid emitting an unresolvable reference.
                continue
            if "urn:uuid:" + uid in kept_urls:
                kept_items.append(item)
        if kept_items:
            resource[field] = kept_items
        else:
            resource.pop(field, None)


def rewrite_refs_in_place(node, url_to_type):
    if isinstance(node, dict):
        ref = node.get("reference")
        if isinstance(ref, str) and ref.startswith("urn:uuid:"):
            uid = ref[len("urn:uuid:"):]
            full_url = "urn:uuid:" + uid
            target_type = url_to_type.get(full_url)
            if target_type is not None:
                node["reference"] = f"{target_type}/{uid}"
            else:
                # Shouldn't happen: repair_references() already removed
                # any reference to a dropped resource before this runs.
                raise ValueError(f"dangling reference after repair: {ref}")
        for value in node.values():
            rewrite_refs_in_place(value, url_to_type)
    elif isinstance(node, list):
        for item in node:
            rewrite_refs_in_place(item, url_to_type)


def convert_post_to_put(entry):
    resource = entry["resource"]
    rt = resource["resourceType"]
    uid = uuid_from_full_url(entry["fullUrl"])
    resource["id"] = uid
    entry["request"] = {"method": "PUT", "url": f"{rt}/{uid}"}
    entry["fullUrl"] = f"{rt}/{uid}"
    return entry


def pull_in_medication_dependencies(kept_urls, by_url):
    """MedicationRequest.medicationReference points at a standalone
    Medication resource; that resource must be kept too or the reference
    dangles. Not selectable on its own -- pulled in only as a dependency.
    """
    added = True
    while added:
        added = False
        for url in list(kept_urls):
            resource = by_url[url]["resource"]
            if resource["resourceType"] != "MedicationRequest":
                continue
            med_ref = resource.get("medicationReference")
            med_uuid = ref_uuid(med_ref)
            if med_uuid is None:
                continue
            med_url = "urn:uuid:" + med_uuid
            if med_url not in kept_urls and med_url in by_url:
                kept_urls.add(med_url)
                added = True


def trim_bundle(bundle, seed, max_resources, min_resources, max_encounters):
    entries = bundle["entry"]
    kept_entries, kept_urls, url_type, by_url = select_bundle(
        entries, seed, max_resources, min_resources, max_encounters
    )
    pull_in_medication_dependencies(kept_urls, by_url)

    # Repair dangling optional refs before rewriting, using the original
    # relative order of entries.
    kept_entries_sorted = [e for e in entries if e["fullUrl"] in kept_urls]
    for entry in kept_entries_sorted:
        repair_references(entry, kept_urls)

    kept_url_type = {url: t for url, t in url_type.items() if url in kept_urls}
    for entry in kept_entries_sorted:
        rewrite_refs_in_place(entry["resource"], kept_url_type)
        convert_post_to_put(entry)

    trimmed = {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": kept_entries_sorted,
    }
    return trimmed


def main():
    parser = argparse.ArgumentParser(
        description="Trim a Synthea R4 transaction bundle to ~15-40 resources, "
        "preserving referential integrity and converting to PUT with client-assigned ids.",
    )
    parser.add_argument("--input", required=True, type=Path, help="Synthea bundle JSON")
    parser.add_argument("--output", required=True, type=Path, help="output path for trimmed bundle")
    parser.add_argument("--seed", required=True, type=int, help="RNG seed for deterministic selection")
    parser.add_argument("--max-resources", type=int, default=35, help="target upper bound on kept resources")
    parser.add_argument("--min-resources", type=int, default=15, help="target lower bound on kept resources")
    parser.add_argument("--max-encounters", type=int, default=5, help="starting number of encounters to keep")
    args = parser.parse_args()

    bundle = json.loads(args.input.read_text())
    if bundle.get("resourceType") != "Bundle":
        print(f"error: {args.input} is not a Bundle", file=sys.stderr)
        sys.exit(1)

    trimmed = trim_bundle(bundle, args.seed, args.max_resources, args.min_resources, args.max_encounters)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(trimmed, indent=2) + "\n")
    counts = {}
    for e in trimmed["entry"]:
        rt = e["resource"]["resourceType"]
        counts[rt] = counts.get(rt, 0) + 1
    print(f"{args.output}: {len(trimmed['entry'])} resources -- {counts}")


if __name__ == "__main__":
    main()
