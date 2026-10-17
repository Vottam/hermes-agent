#!/usr/bin/env python3
"""Acceptance fechado de CUSTOM_03 para a arquitetura de plugin catalog."""
from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import reconcile  # noqa: E402


def fail(message: str) -> int:
    print("CUSTOM_03_ACCEPTANCE=FAIL")
    print(f"CUSTOM_03_ACCEPTANCE_ERROR={message}")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()
    try:
        state = reconcile.validate_repo(repo)
        catalog = state["catalog"]
        if catalog["sha"] not in reconcile.CATALOG_SHAS:
            return fail(f"catalog pin not accepted: {catalog['sha']}")
        if not catalog["supported"]:
            return fail("catalog entry not supported")
        if not state["core_ok"]:
            return fail("core pyproject/uv.lock contain hindsight-client regression")
        pre = state["pre_scan"]
        if pre["pre_resurrection"] != 0:
            return fail(f"anti-resurrection: pre-0.10/embedded hints found ({pre['pre_resurrection']})")
        if state["anti_resurrection_ok"]:
            print("CUSTOM_03_ACCEPTANCE=PASS")
        else:
            return fail("anti-resurrection gate failed")
        print("HINDSIGHT_CONSTRAINT=>=0.10.0,<0.11.0")
        print(f"CATALOG_SHA={catalog['sha']}")
        print(f"CATALOG_VERSION={catalog['version']}")
        print("CORE_HAS_HINDSIGHT_CLIENT=no")
        print("ANTI_RESURRECTION=PASS")
        print("NO_0_6_1_REGRESSION=YES")
        print("NO_0_9_X_REGRESSION=YES")
        print("NO_EMBEDDED_HINT=YES")
        return 0
    except Exception as exc:
        return fail(f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
