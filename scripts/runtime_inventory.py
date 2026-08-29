#!/usr/bin/env python3
"""Emit a normalized, timestamp-free inventory of the locked runtime."""

from __future__ import annotations

import json
import subprocess


PREFIX = "/opt/conda/envs/hummingbot-api"


def main() -> None:
    conda_raw = subprocess.check_output(
        ["/opt/conda/bin/conda", "list", "--prefix", PREFIX, "--json"], text=True
    )
    pip_raw = subprocess.check_output(
        [f"{PREFIX}/bin/python", "-m", "pip", "list", "--format=json", "--disable-pip-version-check"],
        text=True,
    )
    conda = [
        {
            "name": item["name"],
            "version": item["version"],
            "build": item.get("build_string", ""),
            "channel": item.get("channel", ""),
        }
        for item in json.loads(conda_raw)
    ]
    pip = [
        {"name": item["name"].lower(), "version": item["version"]}
        for item in json.loads(pip_raw)
    ]
    inventory = {
        "conda": sorted(conda, key=lambda item: (item["name"], item["version"], item["build"])),
        "pip": sorted(pip, key=lambda item: (item["name"], item["version"])),
    }
    print(json.dumps(inventory, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
