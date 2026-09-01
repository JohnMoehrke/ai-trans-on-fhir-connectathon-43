#!/usr/bin/env python3
"""Offline consistency checks for test_data/, plus manifest verification.

Checks, without needing a running FHIR server:

  - Every JSON file under test_data/ parses, and every bundle is a
    transaction bundle whose entries all use PUT with request.url matching
    resourceType/resource.id.
  - Every reference in every bundle resolves, either to another resource in
    the same bundle or to a resource in the infrastructure bundle.
  - Every AIAST-labeled resource has exactly one Provenance targeting it;
    every AI Provenance's author agent is a known Device, its entity a
    known prompt DocumentReference, and any verifier agent references the
    known Practitioner.
  - The unlabeled bundles contain zero AIAST labels and zero Provenance
    resources.
  - The device -> targets and prompt -> targets maps recomputed from the
    labeled bundles match test_data/manifest.json exactly.
  - Labeled-set distribution rules: every patient has >=2 AI-labeled and
    >=5 unlabeled resources; each device spans >=5 patients with disjoint
    resource sets; each prompt has >=3 Provenances spanning >=2 patients;
    no single device or prompt covers every labeled resource.

Usage:
    python3 validate_data.py [--test-data-dir test_data]
"""
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

WHITELIST = {"Observation", "Condition", "DiagnosticReport", "AllergyIntolerance", "MedicationRequest"}


class Problems:
    def __init__(self):
        self.errors = []

    def add(self, msg):
        self.errors.append(msg)

    def ok(self):
        return not self.errors


def is_labeled(resource):
    for label in resource.get("meta", {}).get("security", []):
        if label.get("code") == "AIAST":
            return True
    return False


def load_bundle(path, problems):
    try:
        data = json.loads(path.read_text())
    except Exception as e:
        problems.add(f"{path}: invalid JSON ({e})")
        return None
    if data.get("resourceType") != "Bundle" or data.get("type") != "transaction":
        problems.add(f"{path}: not a transaction Bundle")
        return None
    for entry in data.get("entry", []):
        resource = entry.get("resource", {})
        req = entry.get("request", {})
        if req.get("method") != "PUT":
            problems.add(f"{path}: entry {entry.get('fullUrl')} is not PUT")
            continue
        expected_url = f"{resource.get('resourceType')}/{resource.get('id')}"
        if req.get("url") != expected_url:
            problems.add(f"{path}: entry request.url {req.get('url')} != {expected_url}")
    return data


def collect_refs(node, out):
    if isinstance(node, dict):
        ref = node.get("reference")
        if isinstance(ref, str) and "/" in ref and not ref.startswith("http") and "?" not in ref:
            out.append(ref)
        for v in node.values():
            collect_refs(v, out)
    elif isinstance(node, list):
        for item in node:
            collect_refs(item, out)


def check_references_resolve(path, bundle, known_urls, problems):
    local_urls = {f"{e['resource']['resourceType']}/{e['resource']['id']}" for e in bundle["entry"]}
    for entry in bundle["entry"]:
        refs = []
        collect_refs(entry["resource"], refs)
        for ref in refs:
            if ref not in local_urls and ref not in known_urls:
                problems.add(f"{path}: dangling reference {ref} in {entry['fullUrl']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-data-dir", default="test_data", type=Path)
    args = parser.parse_args()
    root = args.test_data_dir
    problems = Problems()

    infra_path = root / "infrastructure" / "infrastructure_bundle.json"
    infra = load_bundle(infra_path, problems)
    if infra is None:
        print("FATAL: could not load infrastructure bundle", file=sys.stderr)
        sys.exit(1)

    devices = set()
    prompts = set()
    practitioners = set()
    known_urls = set()
    for e in infra["entry"]:
        r = e["resource"]
        url = f"{r['resourceType']}/{r['id']}"
        known_urls.add(url)
        if r["resourceType"] == "Device":
            devices.add(r["id"])
        elif r["resourceType"] == "DocumentReference":
            prompts.add(r["id"])
        elif r["resourceType"] == "Practitioner":
            practitioners.add(r["id"])
    check_references_resolve(infra_path, infra, set(), problems)

    unlabeled_paths = sorted((root / "unlabeled").glob("*_bundle.json"))
    labeled_paths = sorted((root / "labeled").glob("*_bundle.json"))
    if len(unlabeled_paths) != 2:
        problems.add(f"expected 2 unlabeled bundles, found {len(unlabeled_paths)}")

    for path in unlabeled_paths:
        bundle = load_bundle(path, problems)
        if bundle is None:
            continue
        check_references_resolve(path, bundle, known_urls, problems)
        for e in bundle["entry"]:
            r = e["resource"]
            if r["resourceType"] == "Provenance":
                problems.add(f"{path}: unlabeled bundle must not contain Provenance ({r['id']})")
            if is_labeled(r):
                problems.add(f"{path}: unlabeled bundle must not contain AIAST labels ({r['resourceType']}/{r['id']})")

    # device_id -> set(target refs); prompt_id -> set(target refs)
    device_targets = defaultdict(set)
    prompt_targets = defaultdict(set)
    device_patients = defaultdict(set)
    prompt_patients = defaultdict(set)
    patient_manifest = {}
    all_labeled_targets = set()

    for path in labeled_paths:
        bundle = load_bundle(path, problems)
        if bundle is None:
            continue
        check_references_resolve(path, bundle, known_urls, problems)

        local = {f"{e['resource']['resourceType']}/{e['resource']['id']}": e["resource"] for e in bundle["entry"]}
        patient_entries = [e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == "Patient"]
        if len(patient_entries) != 1:
            problems.add(f"{path}: expected exactly 1 Patient, found {len(patient_entries)}")
            continue
        patient = patient_entries[0]
        patient_url = f"Patient/{patient['id']}"
        name = patient.get("name", [{}])[0]
        display_name = " ".join(name.get("given", [])) + " " + name.get("family", "")

        provenance_targets = defaultdict(list)
        for e in bundle["entry"]:
            r = e["resource"]
            if r["resourceType"] != "Provenance":
                continue
            targets = [t["reference"] for t in r.get("target", [])]
            for t in targets:
                provenance_targets[t].append(r["id"])

            author_devices = []
            verifier_refs = []
            for agent in r.get("agent", []):
                code = agent.get("type", {}).get("coding", [{}])[0].get("code")
                who = agent.get("who", {}).get("reference", "")
                if code == "author":
                    dev_id = who.split("/")[-1]
                    if who.split("/")[0] != "Device" or dev_id not in devices:
                        problems.add(f"{path}: Provenance {r['id']} author is not a known Device: {who}")
                    else:
                        author_devices.append(dev_id)
                elif code == "verifier":
                    if who.split("/")[0] != "Practitioner" or who.split("/")[-1] not in practitioners:
                        problems.add(f"{path}: Provenance {r['id']} verifier is not the known Practitioner: {who}")
                    else:
                        verifier_refs.append(who)
            if len(author_devices) != 1:
                problems.add(f"{path}: Provenance {r['id']} must have exactly 1 author Device agent, found {len(author_devices)}")

            for ent in r.get("entity", []):
                what = ent.get("what", {}).get("reference", "")
                prompt_id = what.split("/")[-1]
                if what.split("/")[0] != "DocumentReference" or prompt_id not in prompts:
                    problems.add(f"{path}: Provenance {r['id']} entity is not a known prompt: {what}")
                    continue
                for t in targets:
                    prompt_targets[prompt_id].add(t)
                    prompt_patients[prompt_id].add(patient_url)

            for dev_id in author_devices:
                for t in targets:
                    device_targets[dev_id].add(t)
                    device_patients[dev_id].add(patient_url)
                    all_labeled_targets.add(t)

        for t, prov_ids in provenance_targets.items():
            if len(prov_ids) != 1:
                problems.add(f"{path}: target {t} is referenced by {len(prov_ids)} Provenances, expected exactly 1")

        ai_labeled = 0
        unlabeled = 0
        for e in bundle["entry"]:
            r = e["resource"]
            if r["resourceType"] in ("Provenance", "Patient"):
                continue
            if is_labeled(r):
                ai_labeled += 1
                url = f"{r['resourceType']}/{r['id']}"
                if url not in provenance_targets:
                    problems.add(f"{path}: AIAST-labeled {url} has no Provenance targeting it")
                if r["resourceType"] not in WHITELIST:
                    problems.add(f"{path}: AIAST label on non-whitelisted type {url}")
            else:
                unlabeled += 1

        if ai_labeled < 2:
            problems.add(f"{path}: only {ai_labeled} AI-labeled resources, need >=2")
        if unlabeled < 5:
            problems.add(f"{path}: only {unlabeled} unlabeled resources, need >=5")

        patient_manifest[patient_url] = {
            "name": display_name.strip(),
            "bundle": f"labeled/{path.name}",
            "ai_labeled": ai_labeled,
            "unlabeled": unlabeled,
        }

    for dev_id, pts in device_patients.items():
        if len(pts) < 5:
            problems.add(f"device {dev_id} spans only {len(pts)} patients, need >=5")
    for prompt_id, pts in prompt_patients.items():
        count = sum(1 for _ in prompt_targets[prompt_id])
        if count < 3:
            problems.add(f"prompt {prompt_id} has only {count} targets, need >=3")
        if len(pts) < 2:
            problems.add(f"prompt {prompt_id} spans only {len(pts)} patients, need >=2")

    # disjointness of device resource sets
    seen_targets = {}
    for dev_id, targets in device_targets.items():
        for t in targets:
            if t in seen_targets and seen_targets[t] != dev_id:
                problems.add(f"target {t} claimed by both {seen_targets[t]} and {dev_id} -- device sets not disjoint")
            seen_targets[t] = dev_id

    total_labeled = len(all_labeled_targets)
    for dev_id, targets in device_targets.items():
        if len(targets) == total_labeled:
            problems.add(f"device {dev_id} covers all {total_labeled} labeled resources")
    for prompt_id, targets in prompt_targets.items():
        if len(targets) == total_labeled:
            problems.add(f"prompt {prompt_id} covers all {total_labeled} labeled resources")

    # manifest diff
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        problems.add(f"{manifest_path} does not exist")
    else:
        manifest = json.loads(manifest_path.read_text())

        def check_section(name, computed_targets, computed_patients=None):
            expected = manifest.get(name, {})
            for key, targets in computed_targets.items():
                exp = expected.get(key)
                if exp is None:
                    problems.add(f"manifest missing {name}.{key}")
                    continue
                if exp["provenance_count"] != len(targets):
                    problems.add(
                        f"manifest {name}.{key}.provenance_count = {exp['provenance_count']}, "
                        f"computed = {len(targets)}"
                    )
                if set(exp["targets"]) != set(targets):
                    problems.add(f"manifest {name}.{key}.targets does not match computed targets")
            for key in expected:
                if key not in computed_targets:
                    problems.add(f"manifest has {name}.{key} but no such data was found in bundles")

        check_section("devices", device_targets)
        check_section("prompts", prompt_targets)

        expected_patients = manifest.get("patients", {})
        if set(expected_patients.keys()) != set(patient_manifest.keys()):
            problems.add("manifest patients keys do not match computed patient set")
        else:
            for pid, exp in expected_patients.items():
                got = patient_manifest[pid]
                if exp != got:
                    problems.add(f"manifest patients.{pid} = {exp} but computed {got}")

    if problems.ok():
        print("OK: all checks passed")
        print(f"  infrastructure: {len(devices)} devices, {len(prompts)} prompts, {len(practitioners)} practitioner(s)")
        print(f"  unlabeled bundles: {len(unlabeled_paths)}")
        print(f"  labeled bundles: {len(labeled_paths)}, {total_labeled} AI-labeled resources total")
        sys.exit(0)
    else:
        print(f"FAILED: {len(problems.errors)} problem(s) found:")
        for p in problems.errors:
            print(f"  - {p}")
        sys.exit(1)


if __name__ == "__main__":
    main()
