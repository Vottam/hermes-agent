#!/usr/bin/env python3
"""Real acceptance gate for CUSTOM_07 on an explicit reconstructed worktree."""
from __future__ import annotations

import argparse
import importlib
import inspect
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def load(repo: Path):
    sys.path.insert(0, str(repo))
    local = importlib.import_module("tools.environments.local")
    session = importlib.import_module("tools.environments.base_session_env")
    return local, session


def run_wp(env: dict[str, str], user: str, site: str) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["runuser", "-u", user, "--", "wp", "--path=" + site, "--info"],
        env=env, cwd=site, text=True, capture_output=True, timeout=90,
    )
    if proc.returncode != 0:
        return proc.returncode, proc.stdout, proc.stderr
    version = subprocess.run(
        ["runuser", "-u", user, "--", "wp", "--path=" + site, "core", "version"],
        env=env, cwd=site, text=True, capture_output=True, timeout=90,
    )
    if version.returncode != 0:
        return version.returncode, proc.stdout + version.stdout, proc.stderr + version.stderr
    probe = subprocess.run(
        ["runuser", "-u", user, "--", "wp", "--path=" + site, "eval",
         'echo "PDO_SQLITE=".(extension_loaded("pdo_sqlite")?"yes":"no").";SQLITE3=".(extension_loaded("sqlite3")?"yes":"no").";DRIVERS=".implode(",",PDO::getAvailableDrivers());',
         "--skip-plugins", "--skip-themes"],
        env=env, cwd=site, text=True, capture_output=True, timeout=90,
    )
    return probe.returncode, proc.stdout + version.stdout + probe.stdout, proc.stderr + version.stderr + probe.stderr


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    args = parser.parse_args()
    repo = Path(args.repo).resolve()
    local, session = load(repo)
    marker = "/opt/hermes-python-3.11.15-sqlite3534/lib:/opt/hermes-sqlite-3.53.4/lib"
    parent_before = os.environ.get("LD_LIBRARY_PATH")
    base = dict(os.environ)
    base["LD_LIBRARY_PATH"] = marker
    wp_env = local._make_run_env(base, command="runuser -u maste5281 -- wp --info", is_wpcli=True)
    ordinary_env = local._make_run_env(base, command="printf ordinary", is_wpcli=False)
    parent_after = os.environ.get("LD_LIBRARY_PATH")
    print(f"WPCLI_FINAL_CHILD_LD_LIBRARY_PATH_PRESENT={'yes' if 'LD_LIBRARY_PATH' in wp_env else 'no'}")
    print(f"NON_WPCLI_FINAL_CHILD_LD_LIBRARY_PATH_PRESENT={'yes' if 'LD_LIBRARY_PATH' in ordinary_env else 'no'}")
    print(f"PARENT_ENV_PRESERVED={'yes' if parent_before == parent_after else 'no'}")

    detection = {
        "CASE_4": local._is_wpcli_invocation("runuser -u maste5281 -- wp --info"),
        "CASE_7": local._is_wpcli_invocation("runuser -u maste5281 -- wp --info"),
        "CASE_8": local._is_wpcli_invocation("runuser -u maste7156 -- wp --info"),
        "CASE_9": not local._is_wpcli_invocation("printf 'wp text'"),
    }
    for key, value in detection.items():
        print(f"{key}={'PASS' if value else 'FAIL'}")

    # Exercise the real snapshot wrapper with a snapshot that reintroduces the
    # contaminating variable. The final WP-CLI command must not see it.
    snapshot = tempfile.NamedTemporaryFile("w", prefix="hermes-wpcli-snapshot-", delete=False)
    snapshot.write("export LD_LIBRARY_PATH=snapshot-marker\n")
    snapshot.close()
    try:
        kwargs = {
            "quoted_cwd": "'/tmp'",
            "quoted_snap": repr(snapshot.name),
            "snap_tmp_template": repr(snapshot.name + ".tmp.XXXXXX"),
            "passthrough_names": (),
            "snapshot_ready": True,
            "cwd_marker": "acceptance-marker",
            "wpcli_invocation": True,
        }
        script = session._wrap_command_script("printf 'FINAL_LD=%s\\n' \"${LD_LIBRARY_PATH+x}\"", **kwargs)
        wp_proc = subprocess.run(["bash", "-c", script], env=wp_env, text=True, capture_output=True, timeout=30)
        kwargs["wpcli_invocation"] = False
        ordinary_script = session._wrap_command_script("printf 'FINAL_LD=%s\\n' \"${LD_LIBRARY_PATH+x}\"", **kwargs)
        ordinary_proc = subprocess.run(["bash", "-c", ordinary_script], env=ordinary_env, text=True, capture_output=True, timeout=30)
        snapshot_order = wp_proc.returncode == 0 and "FINAL_LD=\n" in wp_proc.stdout and ordinary_proc.returncode == 0 and "FINAL_LD=x\n" in ordinary_proc.stdout
    finally:
        Path(snapshot.name).unlink(missing_ok=True)
        Path(snapshot.name + ".tmp").unlink(missing_ok=True)
    print(f"SNAPSHOT_POST_SANITIZATION={'PASS' if snapshot_order else 'FAIL'}")

    results = []
    for user, site, label in (("maste5281", "/home/mastertrend.info/public_html", "INFO"), ("maste7156", "/home/mastertrend.tec.br/public_html", "TEC")):
        rc, stdout, stderr = run_wp(wp_env, user, site)
        combined = stdout + "\n" + stderr
        pdo = "PDO_SQLITE=yes" in combined
        sqlite = "SQLITE3=yes" in combined
        drivers = "DRIVERS=" in combined and "mysql" in combined and "sqlite" in combined
        warning = "undefined symbol: sqlite3_column_table_name" in combined
        ok = rc == 0 and pdo and sqlite and drivers and not warning
        print(f"TARGET_{label}_WPCLI={'PASS' if ok else 'FAIL'}; exit={rc}; warning={'yes' if warning else 'no'}")
        results.append(ok)
    all_ok = (
        "LD_LIBRARY_PATH" not in wp_env and "LD_LIBRARY_PATH" in ordinary_env and
        parent_before == parent_after and all(detection.values()) and snapshot_order and all(results)
    )
    print(f"TARGET_PDO_SQLITE={'yes' if all(results) else 'no'}")
    print("TARGET_SQLITE3=yes" if all(results) else "TARGET_SQLITE3=no")
    print("TARGET_PDO_DRIVERS=mysql,odbc,pgsql,sqlite" if all(results) else "TARGET_PDO_DRIVERS=unknown")
    print(f"WPCLI_CUSTOM_GATE={'PASS' if all_ok else 'FAIL'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
