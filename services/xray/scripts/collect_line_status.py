"""Publish fresh, per-line selection evidence to node_exporter's textfile collector."""
import argparse
import concurrent.futures
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time

import yaml

import line_ha


def observe(config_dir):
    config = json.loads((config_dir / "config.json").read_text())
    balancer = next(b for b in config["routing"]["balancers"] if b["tag"] == line_ha.POOL)
    primary, = balancer["selector"]
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(3)
        # UNIX socket names are limited to 108 bytes. Some line directories have longer names.
        directory = os.open(str(config_dir / ".line-monitor"), os.O_RDONLY | os.O_DIRECTORY)
        try:
            sock.connect("/proc/self/fd/%d/status.sock" % directory)
        finally:
            os.close(directory)
        sock.sendall(b"GET /debug/vars HTTP/1.1\r\nHost: localhost\r\n\r\n")
        response = http.client.HTTPResponse(sock)
        response.begin()
        if response.status != 200:
            raise ValueError("observation unavailable")
        direct = json.load(response)["observatory"][primary]
    observed = int(direct["last_try_time"])
    if observed <= 0:
        raise ValueError("missing probe timestamp")
    return int(not (direct.get("alive", False) and direct.get("delay", 0) < 99999999)), observed


def line_values(base, name, overrides):
    label = '{entry=' + json.dumps(name) + '}'
    profiles = overrides.get("xray-" + name, {}).get("profiles", [])
    if any(str(p).startswith("quota-hold-") for p in profiles):
        result = subprocess.run(["docker", "compose", "ps", "--all", "--format", "{{.State}}", "xray-" + name],
                                cwd=base, capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip() in ("exited", "created"):
            return ["cvgl_line_paused_at_seconds" + label + " " + str(int(time.time()))]
    try:
        route, observed = observe(base / "xray" / name / "config")
    except (OSError, ValueError, KeyError, TypeError, StopIteration, http.client.HTTPException):
        return []
    return ["cvgl_line_uses_backup" + label + " " + str(route),
            "cvgl_line_observed_at_seconds" + label + " " + str(observed)]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    output = parser.parse_args().output
    base = Path(__file__).resolve().parents[2]
    inventory = line_ha.load(base / line_ha.CONFIG)
    overrides = base / "docker-compose.override.yml"
    services = (yaml.safe_load(overrides.read_text()) or {}).get("services", {}) if overrides.is_file() else {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        groups = list(pool.map(lambda r: line_values(base, r, services), inventory["lines"]))
    with tempfile.NamedTemporaryFile(mode="w", dir=output.parent, delete=False) as f:
        for group in groups:
            for row in group:
                print(row, file=f)
    try:
        os.chmod(f.name, 0o644)
        os.replace(f.name, output)
    finally:
        if os.path.exists(f.name):
            os.unlink(f.name)


if __name__ == "__main__":
    main()
