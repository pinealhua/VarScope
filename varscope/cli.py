from __future__ import annotations

import argparse
from pathlib import Path

from varscope.app import run_app
from varscope.paths import default_app_root


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="VarScope")
    parser.add_argument("--elf", type=Path, help="Path to ELF file to preload")
    parser.add_argument("--cfg", type=Path, help="Path to OpenOCD cfg file to preload")
    parser.add_argument(
        "--app-root",
        type=Path,
        default=default_app_root(),
        help="Application root containing runtime/",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run_app(app_root=args.app_root, initial_elf=args.elf, initial_cfg=args.cfg)