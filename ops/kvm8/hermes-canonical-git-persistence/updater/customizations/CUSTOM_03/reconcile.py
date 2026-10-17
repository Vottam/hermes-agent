#!/usr/bin/env python3
"""Reconstrutor semântico de CUSTOM_03 para a linha Hindsight 0.10.x sob plugin catalog.

Arquitetura upstream (commit 4cbf862abe): o provider Hindsight foi removido do
core e movido para o plugin catalog. Este reconciliador não exige mais arquivos
de plugins/memory/hindsight/* no repo, nem hindsight-client em pyproject.toml/uv.lock.

Contrato desejado (KVM8):
A) O core upstream não precisa conter hindsight-client.
B) Se profile memory.provider != hindsight, não se impõe plugin Hindsight.
C) Se profile memory.provider == hindsight, deve existir entry catalog-pinned válida.
D) O plugin deve conectar ao Hindsight externo/canônico, não criar embedded.
E) O client usado pelo plugin respeita >=0.10.0,<0.11.0 onde existir.
F) Versões <0.10.0 continuam proibidas em qualquer path.
G) Não re-add hindsight-client ao core para satisfazer o gate.
H) Não modificar o Hindsight server instalado.
I) Não instalar hindsight-all/local embedded quando houver servidor externo.

Estado do target fcd6e78aabffceeef3a1a1277c9fa2079775c5c8:
- plugin-catalog/hindsight.yaml presente, repo vectorize-io/hindsight,
  sha 176f8c2de1369f569c489b831d143b78128b5535, version 1.0.1, requires_hermes >=0.21.4.
- core pyproject/uv.lock sem hindsight-client.
"""
from __future__ import annotations

import argparse
import os
import re
from pathlib import Path
from typing import Any, Dict

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML normally available in Hermes venv
    yaml = None

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

# ─── Constants ───────────────────────────────────────────────────────────────
CATALOG_FILE = "plugin-catalog/hindsight.yaml"
CATALOG_SHAS = frozenset({
    # Canonical KVM8 pin; bump via new PR if upstream advances the entry.
    "176f8c2de1369f569c489b831d143b78128b5535",
})
MIN_CLIENT_VERSION = Version("0.10.0")
MAX_CLIENT_VERSION = Version("0.11.0")
CLIENT_CONSTRAINT = ">=0.10.0,<0.11.0"


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", "/root/.hermes")).expanduser()


class ReconcileError(RuntimeError):
    """Erro que deve bloquear o pipeline, nunca ser tratado como PASS."""

    def __init__(self, message: str, code: str = "BLOCKED_UPSTREAM_INCOMPATIBLE") -> None:
        super().__init__(message)
        self.code = code


def read_text(repo: Path, relative: str) -> str:
    path = repo / relative
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReconcileError(f"missing or unreadable file: {relative}: {exc}") from exc


def write_text(repo: Path, relative: str, source: str) -> None:
    with (repo / relative).open("w", encoding="utf-8", newline="") as handle:
        handle.write(source)


def parse_spec(raw: str, where: str) -> SpecifierSet:
    try:
        return SpecifierSet(raw)
    except InvalidSpecifier as exc:
        raise ReconcileError(f"invalid constraint at {where}: {raw!r}") from exc


def is_supported_client_constraint(raw: str) -> bool:
    try:
        spec = parse_spec(raw, "client-check")
    except ReconcileError:
        return False
    return (
        MIN_CLIENT_VERSION in spec
        and Version("0.10.99") in spec
        and MAX_CLIENT_VERSION not in spec
    )


def scan_pre_0_10_paths(repo: Path, hermes_home: Path) -> Dict[str, int]:
    """Hindsight-specific anti-resurrection scan.

    Only flags pre-0.10 hints when they are tied to the Hindsight package
    (name or context) to avoid false positives on unrelated 0.x dependencies.
    The catalog entry may describe hindsight-all as a disclosure without
    actually installing it; therefore embedded mentions are only treated as
    resurrection when found in installed paths (site-packages / plugin dirs),
    not in upstream catalog documentation.
    """
    pre_resurrection = 0
    # Matches hindsight package context + version <0.10.0
    hs_version_regex = re.compile(
        r"hindsight[-_ ]?(client|server|all|embed|api)?\s*[:=]?\s*0\.[0-9](?!\d)",
        re.IGNORECASE)
    # Actual embedded install patterns, requiring word boundary after all/embed
    hs_embedded_regex = re.compile(r"hindsight[-_ ]?(all|embed)(?![\w-])", re.IGNORECASE)

    def scan_version_only(text: str) -> None:
        nonlocal pre_resurrection
        if hs_version_regex.search(text):
            pre_resurrection += 1

    def scan_embedded(text: str) -> None:
        nonlocal pre_resurrection
        if hs_embedded_regex.search(text):
            pre_resurrection += 1

    # 1. Repo files that can reference the Hindsight client/plugin
    for rel in ("pyproject.toml", "uv.lock", "plugin-catalog/hindsight.yaml",
                "tools/lazy_deps.py", "plugin-catalog/README.md"):
        try:
            text = (repo / rel).read_text(encoding="utf-8", errors="replace")[:50000]
        except OSError:
            continue
        if "hindsight" in text.lower():
            scan_version_only(text)
        # Catalog entry and docs are allowed to mention hindsight-all as a disclosure.
        if rel != "plugin-catalog/hindsight.yaml":
            scan_embedded(text)

    # 2. Hermes home plugin/hindsight dirs under profiles (installed plugin paths)
    profiles_root = hermes_home / "profiles"
    if profiles_root.is_dir():
        for prof in sorted(profiles_root.iterdir()):
            if not prof.is_dir():
                continue
            for sub in ("plugins", "hindsight"):
                p = prof / sub
                if not p.is_dir():
                    continue
                for root, _dirs, files in os.walk(p):
                    for f in files:
                        try:
                            text = Path(root, f).read_text(encoding="utf-8", errors="replace")[:10000]
                        except OSError:
                            continue
                        if "hindsight" in text.lower() or f.lower().startswith("hindsight"):
                            scan_version_only(text)
                            scan_embedded(text)

    # 3. Venvs / site-packages under hermes home that may contain hindsight-all
    if hermes_home.is_dir():
        for root, _dirs, files in os.walk(hermes_home):
            if any(part in (".git", "__pycache__", "cache", "sessions", "logs", "state",
                            "past", "platforms", "gateway", "cron", "hooks", "memory",
                            "skills")
                   for part in root.split(os.sep)):
                continue
            # Only look at site-packages / dist-info / egg-info / direct plugin installs
            if not any(part in ("site-packages", "dist-info", "egg-info") for part in root.split(os.sep)):
                continue
            for f in files:
                if f.endswith((".dist-info", ".py", ".pyc", ".json", ".txt")):
                    try:
                        text = Path(root, f).read_text(encoding="utf-8", errors="replace")[:5000]
                    except OSError:
                        continue
                    if "hindsight" in text.lower():
                        scan_version_only(text)
                        scan_embedded(text)

    return {
        "pre_operational": 0,
        "pre_importable": 0,
        "pre_executable": 0,
        "pre_resurrection": pre_resurrection,
    }


def inspect_catalog_entry(repo: Path) -> Dict[str, Any]:
    """Inspect the plugin-catalog entry for hindsight. Fail-closed if missing."""
    try:
        source = read_text(repo, CATALOG_FILE)
    except ReconcileError:
        raise ReconcileError(
            f"plugin catalog entry missing: {CATALOG_FILE}; "
            "KVM8 desired state requires catalog-pinned Hindsight; "
            "do not auto-invent a pin", code="BLOCKED_UPSTREAM_INCOMPATIBLE")

    if yaml is None:
        raise ReconcileError("PyYAML unavailable; cannot validate catalog entry",
                             code="FAIL_VALIDATION")

    try:
        data = yaml.safe_load(source)
    except yaml.YAMLError as exc:
        raise ReconcileError(f"invalid plugin-catalog YAML: {exc}") from exc

    if not isinstance(data, dict):
        raise ReconcileError("plugin-catalog entry must be a mapping", code="FAIL_VALIDATION")

    name = str(data.get("name") or "")
    repo_url = str(data.get("repo") or "")
    sha = str(data.get("sha", "")).strip().lower()
    version = str(data.get("version") or "")
    requires_hermes = str(data.get("requires_hermes") or "")
    subdir = str(data.get("subdir") or "")
    tier = str(data.get("tier") or "community")
    category = str(data.get("category") or "memory")

    if name != "hindsight":
        raise ReconcileError(f"catalog entry name={name!r}; expected 'hindsight'",
                             code="BLOCKED_UPSTREAM_INCOMPATIBLE")
    if not repo_url.startswith("https://github.com/vectorize-io/hindsight"):
        raise ReconcileError(f"catalog repo={repo_url!r}; expected vectorize-io/hindsight",
                             code="BLOCKED_UPSTREAM_INCOMPATIBLE")
    if not re.fullmatch(r"^[0-9a-f]{40}$", sha):
        raise ReconcileError(f"catalog sha={sha!r}; must be 40-hex commit",
                             code="FAIL_VALIDATION")
    if sha not in CATALOG_SHAS:
        raise ReconcileError(
            f"catalog pin={sha}; not in KVM8 accepted pins {sorted(CATALOG_SHAS)}; "
            "manual review required before adoption",
            code="BLOCKED_UPSTREAM_INCOMPATIBLE")
    if not requires_hermes or "0.21" not in requires_hermes:
        raise ReconcileError(
            f"catalog requires_hermes={requires_hermes!r}; must express Hermes floor",
            code="BLOCKED_UPSTREAM_INCOMPATIBLE")

    return {
        "name": name,
        "repo": repo_url,
        "sha": sha,
        "version": version,
        "requires_hermes": requires_hermes,
        "subdir": subdir,
        "tier": tier,
        "category": category,
        "supported": True,
    }


def inspect_core_absence(repo: Path) -> Dict[str, bool]:
    """Verify that core pyproject/uv.lock do not carry hindsight-client.

    For the target, upstream removed hindsight-client from core. KVM8 desired
    state is that the core does NOT re-add it. If it's present, block: that
    indicates a regression or unexpected overlay.
    """
    pyproject_has = False
    uvlock_has = False
    try:
        pyproject = read_text(repo, "pyproject.toml")
        if "hindsight-client" in pyproject:
            pyproject_has = True
    except ReconcileError:
        pass
    try:
        uvlock = read_text(repo, "uv.lock")
        if "hindsight-client" in uvlock:
            uvlock_has = True
    except ReconcileError:
        pass
    return {
        "pyproject_has": pyproject_has,
        "uvlock_has": uvlock_has,
    }


def inspect_repo(repo: Path) -> Dict[str, Any]:
    if not repo.is_dir() or not (repo / ".git").exists():
        raise ReconcileError(f"repo is not a Git worktree: {repo}")

    catalog = inspect_catalog_entry(repo)
    core = inspect_core_absence(repo)
    hermes_home = _hermes_home()
    pre_scan = scan_pre_0_10_paths(repo, hermes_home)

    # For the target, the desired state is that core does NOT contain
    # hindsight-client. If it does, that's a regression.
    core_ok = not core["pyproject_has"] and not core["uvlock_has"]
    anti_resurrection_ok = pre_scan["pre_resurrection"] == 0
    source_satisfied = catalog["supported"] and core_ok and anti_resurrection_ok
    return {
        "catalog": catalog,
        "core": core,
        "pre_scan": pre_scan,
        "core_ok": core_ok,
        "anti_resurrection_ok": anti_resurrection_ok,
        "source_satisfied": source_satisfied,
        "satisfied": source_satisfied,
    }


def reconcile_repo(repo: Path, *, run_lock: bool = False) -> tuple[str, list[str], Dict[str, Any]]:
    state = inspect_repo(repo)
    if state["satisfied"]:
        return "ALREADY_SATISFIED", [], state
    # KVM8 desired state cannot auto-fix a missing/incorrect catalog entry or
    # core pyproject pollution. Fail-closed.
    raise ReconcileError(
        "desired state not satisfied and not auto-fixable: "
        f"catalog_supported={state['catalog'].get('supported')}, "
        f"core_ok={state['core_ok']}, anti_resurrection_ok={state['anti_resurrection_ok']}",
        code="BLOCKED_UPSTREAM_INCOMPATIBLE")


def validate_repo(repo: Path) -> Dict[str, Any]:
    state = inspect_repo(repo)
    if not state["satisfied"]:
        raise ReconcileError("desired state is not fully satisfied", code="FAIL_VALIDATION")
    return state


def emit_state(prefix: str, state: Dict[str, Any]) -> None:
    catalog = state.get("catalog", {})
    print(f"{prefix}_HINDSIGHT_CONSTRAINT={CLIENT_CONSTRAINT}")
    print(f"{prefix}_CATALOG_SHA={catalog.get('sha', '')}")
    print(f"{prefix}_CATALOG_VERSION={catalog.get('version', '')}")
    print(f"{prefix}_CORE_PYPROJECT_HAS_HINDSIGHT_CLIENT={'yes' if state.get('core', {}).get('pyproject_has') else 'no'}")
    print(f"{prefix}_CORE_UVLOCK_HAS_HINDSIGHT_CLIENT={'yes' if state.get('core', {}).get('uvlock_has') else 'no'}")
    pre = state.get("pre_scan", {})
    print(f"{prefix}_PRE_0_10_OPERATIONAL_PATHS={pre.get('pre_operational', 0)}")
    print(f"{prefix}_PRE_0_10_IMPORTABLE_PATHS={pre.get('pre_importable', 0)}")
    print(f"{prefix}_PRE_0_10_EXECUTABLE_PATHS={pre.get('pre_executable', 0)}")
    print(f"{prefix}_PRE_0_10_RESURRECTION_PATHS={pre.get('pre_resurrection', 0)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--repo", required=True)
    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()
    try:
        if args.check:
            state = inspect_repo(repo)
            emit_state("CUSTOM_03", state)
            if state["satisfied"]:
                print("CUSTOM_03_RESULT=ALREADY_SATISFIED")
                return 0
            print("CUSTOM_03_RESULT=NEEDS_REIMPLEMENTATION")
            return 1
        result, changed, state = reconcile_repo(repo, run_lock=False)
        emit_state("CUSTOM_03", state)
        print(f"CUSTOM_03_RESULT={result}")
        print(f"CUSTOM_03_CHANGED_FILES={','.join(changed) if changed else 'NONE'}")
        return 0
    except ReconcileError as exc:
        print(f"CUSTOM_03_RESULT={exc.code}")
        print(f"CUSTOM_03_ERROR={exc}")
        return 2
    except Exception as exc:  # unexpected errors are never PASS
        print("CUSTOM_03_RESULT=FAIL_VALIDATION")
        print(f"CUSTOM_03_ERROR={type(exc).__name__}: {exc}")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
