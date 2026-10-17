#!/usr/bin/env python3
"""CUSTOM_08 tests — prova que o reconciler DETECTA e REPARA estados divergentes.

Nao toca nos wrappers reais: cada caso roda sobre um WRAPPER_DIR temporario com
HERMES_ROOT_OVERRIDE, e verifica apenas a logica de inspect/evaluate/apply.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

RECONCILE = Path(__file__).with_name("reconcile.py")
PM = str(Path(os.environ.get("HERMES_CANONICAL_REPO", "<CANONICAL_REPO>")) / ".hermes" / "bin" / "hermes")

spec = importlib.util.spec_from_file_location("c08", RECONCILE)
if spec is None or spec.loader is None:  # pragma: no cover
    raise RuntimeError(f"cannot load reconciler from {RECONCILE}")
c08 = importlib.util.module_from_spec(spec)
# `dataclasses.dataclass` resolve o namespace do modulo via sys.modules[cls.__module__];
# sem este registro o import falha com AttributeError em Python 3.11.
sys.modules[spec.name] = c08
spec.loader.exec_module(c08)

PROFILES_ROOT = Path("/root/.hermes/profiles")
LIVE = sorted(p.name for p in PROFILES_ROOT.iterdir() if p.is_dir())


class WrapperDirCase(unittest.TestCase):
    """Base: cada teste recebe um ~/.local/bin isolado."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self._orig_dir = c08.WRAPPER_DIR
        c08.WRAPPER_DIR = self.dir
        self.addCleanup(lambda: setattr(c08, "WRAPPER_DIR", self._orig_dir))

        self.install_root = Path("/opt/hermes-agent-clean")
        self._orig_pm = c08._pm_launcher
        c08._pm_launcher = lambda root: self.install_root / ".hermes" / "bin" / "hermes"
        self.addCleanup(lambda: setattr(c08, "_pm_launcher", self._orig_pm))

    def write(self, name: str, body: str) -> Path:
        p = self.dir / name
        p.write_text(body, encoding="utf-8")
        p.chmod(0o755)
        return p

    def states(self):
        return [c08.inspect(p, self.install_root) for p in c08.discover_wrappers()]

    def evaluate(self):
        return c08.evaluate(self.states(), self.install_root)

    def good(self, profile: str, shebang: str = "#!/usr/bin/env bash") -> str:
        return (
            f"{shebang}\n"
            f'PROFILE="{profile}"\n'
            'export HERMES_HOME="/root/.hermes/profiles/${PROFILE}"\n'
            f'exec {PM} -p {profile} "$@"\n'
        )


class TestHealthy(WrapperDirCase):
    def test_pm_direct_literal_passes(self):
        self.write("hermes-kvm8", self.good("hermes-kvm8"))
        self.assertEqual(self.evaluate(), [])

    def test_all_live_profiles_pass_together(self):
        for name in LIVE:
            self.write(f"hermes-{name}", self.good(name))
        self.assertEqual(self.evaluate(), [])

    def test_sh_preserved_by_desired_content(self):
        state = c08.inspect(
            self.write("hermes-x", self.good("hermes-x", "#!/bin/sh")),
            self.install_root,
        )
        self.assertTrue(c08.desired_content(state, self.install_root).startswith("#!/bin/sh"))


class TestViolations(WrapperDirCase):
    def test_legacy_venv_exec_is_caught(self):
        self.write(
            "hermes-kvm8",
            '#!/usr/bin/env bash\nPROFILE="hermes-kvm8"\n'
            'export HERMES_HOME="/root/.hermes/profiles/${PROFILE}"\n'
            'exec /root/.hermes/venvs/hermes-kvm8-canonical/bin/hermes "$@"\n',
        )
        problems = self.evaluate()
        self.assertTrue(any("LEGACY_VENV_EXEC" in p for p in problems), problems)

    def test_local_venv_exec_is_caught(self):
        self.write(
            "hermes-kvm8",
            '#!/bin/sh\nPROFILE="hermes-kvm8"\n'
            'export HERMES_HOME="/root/.hermes/profiles/${PROFILE}"\n'
            'exec /root/.hermes/profiles/hermes-kvm8/.venv/bin/hermes "$@"\n',
        )
        self.assertTrue(any("LOCAL_VENV_EXEC" in p for p in self.evaluate()))

    def test_generic_p_is_caught(self):
        body = self.good("hermes-kvm8").replace(f"exec {PM} -p hermes-kvm8", "exec hermes -p hermes-kvm8")
        self.write("hermes-kvm8", body)
        self.assertTrue(any("NOT_PM_DIRECT" in p for p in self.evaluate()))

    def test_non_literal_minus_p_is_caught(self):
        body = self.good("hermes-kvm8").replace("-p hermes-kvm8", '-p "$PROFILE"')
        self.write("hermes-kvm8", body)
        problems = self.evaluate()
        self.assertTrue(any("NON_LITERAL_MINUS_P" in p for p in problems), problems)

    def test_generic_root_hermes_home_is_caught(self):
        body = self.good("hermes-kvm8").replace(
            '"/root/.hermes/profiles/${PROFILE}"', '"/root/.hermes"'
        )
        self.write("hermes-kvm8", body)
        self.assertTrue(any("GENERIC_ROOT_HERMES_HOME" in p for p in self.evaluate()))

    def test_missing_hermes_home_is_caught(self):
        body = (
            "#!/bin/sh\n"
            'PROFILE="hermes-kvm8"\n'
            f'exec {PM} -p hermes-kvm8 "$@"\n'
        )
        self.write("hermes-kvm8", body)
        self.assertTrue(any("NO_HERMES_HOME_EXPORT" in p for p in self.evaluate()))

    def test_home_of_another_profile_is_caught(self):
        body = self.good("hermes-seo8").replace("profiles/${PROFILE}", "profiles/hermes-kvm8")
        self.write("hermes-seo8", body)
        self.assertTrue(any("WRONG_HERMES_HOME" in p for p in self.evaluate()))

    def test_minus_p_profile_mismatch_is_caught(self):
        body = self.good("hermes-kvm8").replace("-p hermes-kvm8", "-p hermes-seo8")
        self.write("hermes-kvm8", body)
        self.assertTrue(any("MINUS_P_PROFILE_MISMATCH" in p for p in self.evaluate()))

    def test_nonexistent_profile_home_is_caught(self):
        body = self.good("hermes-kvm8").replace("profiles/${PROFILE}", "profiles/hermes-nao-existe-xyz")
        self.write("hermes-kvm8", body)
        self.assertTrue(any("PROFILE_HOME_MISSING" in p for p in self.evaluate()))


class TestExclusions(WrapperDirCase):
    def test_rollback_artifacts_are_not_targets(self):
        stamp = "20260926T051113Z"
        for n in (f"hermes-kvm8.pre-pm-{stamp}", f"hermes-seo8.pre-pm-{stamp}"):
            self.write(n, self.good("hermes-kvm8").replace("hermes-kvm8", "x"))
        self.assertEqual(c08.profile_wrappers(self.states()), [])

    def test_runtime_launcher_is_not_a_profile_wrapper(self):
        self.write("hermes-agent", "#!/bin/sh\nexec /opt/x/.hermes/bin/hermes --run-module run_agent \"$@\"\n")
        self.assertEqual(c08.profile_wrappers(self.states()), [])

    def test_rollback_artifact_violation_is_invisible_to_gate(self):
        # Um backup LEGACY nao pode fazer o gate falhar nem ser reescrito.
        stamp = "20260926T051113Z"
        self.write(
            f"hermes-kvm8.pre-pm-{stamp}",
            '#!/bin/sh\nPROFILE="hermes-kvm8"\n'
            "exec /root/.hermes/venvs/hermes-kvm8-canonical/bin/hermes \"$@\"\n",
        )
        self.write("hermes-kvm8", self.good("hermes-kvm8"))
        self.assertEqual(self.evaluate(), [])

    def test_binary_file_does_not_crash_discovery(self):
        (self.dir / "uv").write_bytes(b"\x00\xf8" + os.urandom(64))
        self.write("hermes-kvm8", self.good("hermes-kvm8"))
        self.assertEqual(self.evaluate(), [])

    def test_unreadable_wrapper_fails_closed(self):
        p = self.write("hermes-kvm8", self.good("hermes-kvm8"))
        p.chmod(0o000)
        self.addCleanup(lambda: p.chmod(0o755))
        if os.geteuid() == 0:
            self.skipTest("root ignora bit de permissao: UNREADABLE nao e testavel como root")
        self.assertTrue(any("UNREADABLE" in x for x in self.evaluate()))


class TestLegitimateAliases(WrapperDirCase):
    def test_two_aliases_of_same_profile_is_allowed(self):
        self.write("hermes-kvm8", self.good("hermes-kvm8"))
        self.write("hermes-statusbar", self.good("hermes-kvm8"))
        self.assertEqual(self.evaluate(), [])

    def test_two_profiles_sharing_another_profile_home_is_caught(self):
        body = self.good("hermes-seo8").replace("profiles/${PROFILE}", "profiles/hermes-kvm8")
        self.write("hermes-seo8", body)
        self.write("hermes-forte", body.replace("hermes-seo8", "hermes-forte"))
        problems = self.evaluate()
        self.assertTrue(any("WRONG_HERMES_HOME" in p for p in problems), problems)


class TestApply(WrapperDirCase):
    def test_apply_repairs_and_backs_up(self):
        self.write(
            "hermes-kvm8",
            '#!/bin/sh\nPROFILE="hermes-kvm8"\n'
            'export HERMES_HOME="/root/.hermes/profiles/${PROFILE}"\n'
            'exec /root/.hermes/venvs/hermes-kvm8-canonical/bin/hermes "$@"\n',
        )
        os.environ["CUSTOM_08_STAMP"] = "20260926T000000Z"
        self.addCleanup(os.environ.pop, "CUSTOM_08_STAMP", None)
        changed = c08.apply(self.states(), self.install_root, stamp="20260926T000000Z")
        self.assertEqual(changed, 1)
        text = (self.dir / "hermes-kvm8").read_text(encoding="utf-8")
        self.assertIn(f"exec {PM} -p hermes-kvm8", text)
        self.assertTrue((self.dir / "hermes-kvm8.pre-pm-20260926T000000Z").exists())
        self.assertTrue((self.dir / "hermes-kvm8").stat().st_mode & 0o111)
        self.assertEqual(self.evaluate(), [])

    def test_apply_is_idempotent(self):
        self.write("hermes-kvm8", self.good("hermes-kvm8"))
        self.assertEqual(
            c08.apply(self.states(), self.install_root, stamp="20260926T000000Z"), 0
        )
        self.assertFalse((self.dir / "hermes-kvm8.pre-pm-20260926T000000Z").exists())


if __name__ == "__main__":
    print(f"LIVE_PROFILES={LIVE}")
    unittest.main(verbosity=2)
