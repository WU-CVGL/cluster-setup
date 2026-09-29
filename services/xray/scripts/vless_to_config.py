#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Read a vless:// share URL and emit a standalone Xray config JSON.

This script intentionally does not touch docker-compose, Prometheus, service
directories, or any cluster/deployment files.

To use the output as services/xray/<name>/config/config.json of an existing
compose service, pass --stats-api (the exporter needs the stats API) and keep
the default --http-port/--socks-port: they are the ports inside the container
(8889/1089), not the host ports published in docker-compose.yml.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from XrayConfigHandler import DEFAULT_API_PORT, DEFAULT_HTTP_PORT, DEFAULT_SOCKS_PORT, XrayConfigHandler


def read_vless_url(vless_url: Optional[str], input_file: Optional[str]) -> str:
    if vless_url and input_file:
        raise ValueError("Use either a vless URL argument or --input-file, not both.")

    if input_file:
        value = Path(input_file).read_text(encoding="utf-8").strip()
    elif vless_url:
        value = vless_url.strip()
    elif not sys.stdin.isatty():
        value = sys.stdin.read().strip()
    else:
        raise ValueError("Missing vless URL. Pass it as an argument, via --input-file, or stdin.")

    if not value:
        raise ValueError("The vless URL is empty.")
    if not value.startswith("vless://"):
        raise ValueError("Only vless:// URLs are supported by this script.")

    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read a vless:// URL and output a standalone Xray config JSON.",
    )
    parser.add_argument(
        "vless_url",
        nargs="?",
        help="vless:// share URL. If omitted, stdin is used.",
    )
    parser.add_argument(
        "-i",
        "--input-file",
        help="Read the vless:// URL from a text file.",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Write config JSON to this file. Defaults to stdout.",
    )
    parser.add_argument(
        "--http-port",
        type=int,
        default=DEFAULT_HTTP_PORT,
        help=f"HTTP inbound port in the generated config. Default: {DEFAULT_HTTP_PORT}.",
    )
    parser.add_argument(
        "--socks-port",
        type=int,
        default=DEFAULT_SOCKS_PORT,
        help=f"SOCKS inbound port in the generated config. Default: {DEFAULT_SOCKS_PORT}.",
    )
    parser.add_argument(
        "--loglevel",
        default="warning",
        choices=["debug", "info", "warning", "error", "none"],
        help="Xray log level. Default: warning.",
    )
    parser.add_argument(
        "--indent",
        type=int,
        default=4,
        help="JSON indentation. Default: 4.",
    )
    parser.add_argument(
        "--stats-api",
        action="store_true",
        help=(
            "Also emit the stats/api/policy sections, the dokodemo-door 'api' inbound "
            f"on port {DEFAULT_API_PORT} and its routing rule, which the v2ray exporter "
            "in docker-compose.yml needs. Default: off."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        vless_url = read_vless_url(args.vless_url, args.input_file)
        parsed_config = XrayConfigHandler.parse_vless_url(vless_url)
        outbound = XrayConfigHandler.vless_to_xray_outbound(parsed_config)
        xray_config = XrayConfigHandler.build_xray_config(
            outbound=outbound,
            http_port=args.http_port,
            socks_port=args.socks_port,
            loglevel=args.loglevel,
            include_stats_api=args.stats_api,
        )
        output = json.dumps(xray_config, indent=args.indent, ensure_ascii=False)

        if args.output:
            output_path = Path(args.output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(output + "\n", encoding="utf-8")
            print(f"Wrote config: {output_path}", file=sys.stderr)
        else:
            print(output)

        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())