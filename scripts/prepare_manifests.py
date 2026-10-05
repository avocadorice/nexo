"""Split a kubectl JSON render so migrations finish before new workloads roll out."""

import json
import sys
from pathlib import Path


def split(objects):
    groups = {"prerequisites": [], "migration": [], "workloads": []}
    for resource in objects:
        kind = resource["kind"]
        if kind == "Job" and resource["metadata"]["name"] == "migrate":
            group = "migration"
        elif kind in ("Deployment", "StatefulSet", "CronJob", "Job"):
            group = "workloads"
        else:
            group = "prerequisites"
        groups[group].append(resource)
    if len(groups["migration"]) != 1:
        raise ValueError("deployment must contain exactly one migration Job")
    return groups


def main():
    destination = Path(sys.argv[1])
    destination.mkdir(parents=True, exist_ok=True)
    raw = sys.stdin.read().lstrip()
    decoder = json.JSONDecoder()
    objects = []
    while raw:
        manifest, end = decoder.raw_decode(raw)
        objects.extend(manifest["items"] if manifest["kind"] == "List" else [manifest])
        raw = raw[end:].lstrip()
    for name, resources in split(objects).items():
        (destination / f"{name}.json").write_text(
            json.dumps(
                {
                    "apiVersion": "v1",
                    "kind": "List",
                    "items": resources,
                }
            )
        )


if __name__ == "__main__":
    main()
