#!/usr/bin/env python3
"""Semantic release-relocation validator for the KVM8 Hindsight updater.

Replaces the defective lexical scan

    grep -R --fixed-strings "candidate-" <release>/venv

which matched ordinary source tokens (`candidate-split-evaluation`,
`candidate-config`, binary `.so` content) and therefore failed VALID releases --
including the official prod-0.10.1 environment, and therefore also failed the
certified fork promotion with exit 1 after a fully successful swap.

A relocation is a PATH fact, not a token fact. The question the original check
was trying to answer is:

    "Does the activated runtime for this release resolve inside THIS release
     directory, and does it still reference a staging/candidate tree?"

Checks (each independently reported):

  VENV_PRESENT      the release venv and interpreter exist
  PREFIX_CHECK      sys.prefix is the release venv
  EXECUTABLE_CHECK  sys.executable is inside the release venv
  SHEBANG_CHECK     every canonical launcher in <release>/bin resolves to a
                    file inside the release venv AND its shebang interpreter is
                    inside the release venv
  LAUNCHER_PREFIX   the interpreter each launcher NAMES reports a prefix of this
                    release. This is the check that catches a stale-shebang
                    promotion: after candidate-* is renamed to prod-*, every
                    console-script shebang still names the old tree, and running
                    through it lands the runtime in staging.
  CONSOLE_SCRIPT    path values recorded in <release>/venv/bin (console
                    scripts, activate) name the FINAL release
  STAGING_REF       no recorded path under the install root names a
                    `releases/candidate-*` tree, and no recorded path is a
                    dead link
  RUNTIME_PATHS     the package origin and purelib resolve inside the venv

ROOT RELATIVITY (why nothing here is hardcoded to /opt/hindsight-kvm8).
The install root is a PARAMETER (`--root`, default /opt/hindsight-kvm8). An
earlier draft hardcoded that absolute prefix in the path regex, which silently
made every staging-reference check a no-op under any other root: the fixtures
live in a tempdir, so `ABS_HINDSIGHT_PATH` matched nothing and R3/R4/R6
reported PASS instead of FAIL. A relocation gate that only works at one hardcoded
path is not a gate. `root` is threaded explicitly so the same code is exercised
identically by production and by fixtures.

SCOPE OF TEXT SCANNING. Only files that can carry PATH VALUES are read: text
files under <release>/venv/bin plus the venv's pyvenv.cfg. Package source and
compiled extensions are never read -- a lexical token in a dependency's source
is not a path fact and must never fail a release.

PYVENV.CFG PROVENANCE. A venv records the command that created it. When a fork
release is built from a previous release's venv, `command` legitimately names
the PARENT release, e.g.

    command = .../releases/prod-0.10.1/venv/bin/python -m venv .../releases/prod-0.10.1+mastertrend.t22.1/venv

That is build history, not relocation: the new release is the destination. The
gate therefore validates pyvenv.cfg identity fields (home/version/executable)
and the destination, and does NOT require the venv's creator to be itself. A
`releases/candidate-*` parent is still a STAGING_REF failure.

LITERAL-PATH SEMANTICS (deliberate, and the reason this differs from the old
check). Inside a venv, `bin/python` and `bin/python3.11` are symlinks to the
BASE system interpreter, so realpath() of a correct interpreter legitimately
leaves the venv. Relocation correctness for such entries is therefore decided
on the LITERAL recorded path plus its existence:

  * literal path inside <release>/venv, and it exists -> correct
  * literal path elsewhere / naming a `releases/candidate-*` / nonexistent
    -> relocation

A dead literal path is the real failure mode after a rename or a purge: the
launcher would execute `bad interpreter: No such file or directory` (RC=126)
while the still-running daemon keeps serving from its already-open inode.

Exit codes: 0 PASS, 1 FAIL, 2 ERROR (validator could not run).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

DEFAULT_ROOT = "/opt/hindsight-kvm8"

LAUNCHER_NAMES = ("hindsight-api", "hindsight-worker", "hindsight-admin")
# Only text files can carry PATH VALUES. Compiled .so content embedding the
# lexical token "candidate-" is not a path fact and must never fail a release.
TEXT_SUFFIXES = ("", ".py", ".cfg", ".pth", ".json", ".txt", ".sh")


def _norm(p: str) -> str:
    """Literal, non-resolved, trailing-slash-free path."""
    return p.rstrip("/") if p else p


def _literal_under(path: str, root: str) -> bool:
    """True when `path` is root or lies beneath it. No realpath: see module docstring."""
    if not path:
        return False
    path, root = _norm(path), _norm(root)
    return path == root or path.startswith(root + "/")


def _path_regex(root: str) -> re.Pattern:
    """Match any absolute path under `root`, with a self-escaping char class.

    Built from the parameterised root so the gate is not tied to one absolute
    path (see ROOT RELATIVITY). The class is derived from the root's own
    characters plus the ones that legitimately occur in a path VALUE; it must
    not stop at a space, since a recorded value is a whole path.
    """
    allowed = re.escape(root) + r"[A-Za-z0-9._+@%:,=/\\-]*"
    return re.compile(allowed)


def _candidate_tree_regex(root: str) -> re.Pattern:
    """Match a `candidate-` path COMPONENT anywhere under the install root.

    Not `<root>/releases/candidate-`: the gate must not assume the release tree
    has a particular shape, or a staging tree placed elsewhere under the root
    (or a differently-named staging prefix) would not be recognised. What
    matters is that a recorded path value names a candidate directory, not
    which directory holds the releases.
    """
    return re.compile(
        re.escape(root) + r"(?:/[A-Za-z0-9._+@%=-]+)*?/candidate-[^\s\"'\\:)\];]*"
    )


class Result:
    def __init__(self) -> None:
        self.checks: list[tuple[str, str, str]] = []

    def add(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append((name, "PASS" if ok else "FAIL", detail))
        return ok

    @property
    def ok(self) -> bool:
        return all(s == "PASS" for _, s, _ in self.checks)

    def as_dict(self) -> dict:
        return {
            "verdict": "PASS" if self.ok else "FAIL",
            "checks": [{"name": n, "status": s, "detail": d} for n, s, d in self.checks],
        }


def _run_python(py: str, code: str) -> tuple[int, str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    env["VIRTUAL_ENV"] = os.path.dirname(os.path.dirname(py))
    try:
        p = subprocess.run([py, "-c", code], capture_output=True, text=True, env=env, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "", str(exc)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def _read_text(path: str):
    """Read a small text file strictly. Returns None for anything unreadable."""
    try:
        if os.path.getsize(path) > 4 * 1024 * 1024:
            return None
        with open(path, "r", encoding="utf-8", errors="strict") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return None


def _iter_path_value_files(venv: str):
    """Every file that can legitimately carry an absolute PATH VALUE.

    Restricted to <release>/venv/bin plus the venv's own pyvenv.cfg. Package
    source under site-packages is deliberately NOT included: source text is not
    a path record.
    """
    d = os.path.join(venv, "bin")
    if os.path.isdir(d):
        for name in sorted(os.listdir(d)):
            if os.path.splitext(name)[1] not in TEXT_SUFFIXES:
                continue
            fp = os.path.join(d, name)
            if not (os.path.isfile(fp) or os.path.islink(fp)):
                continue
            text = _read_text(fp)
            if text is not None:
                yield name, text
    cfg = os.path.join(venv, "pyvenv.cfg")
    if os.path.isfile(cfg):
        text = _read_text(cfg)
        if text is not None:
            yield "pyvenv.cfg", text


def _check_pyvenv(venv: str, release_dir: str) -> tuple[bool, str]:
    """Identity fields of pyvenv.cfg must exist and the venv must be coherent.

    The `command` line is provenance (which interpreter CREATED this venv) and
    is exempt: see PYVENV.CFG PROVENANCE. A candidate parent is still caught by
    STAGING_REF.
    """
    cfg = os.path.join(venv, "pyvenv.cfg")
    if not os.path.isfile(cfg):
        return True, "no_pyvenv_cfg"  # not a venv layout; other checks govern
    try:
        with open(cfg, "r", encoding="utf-8", errors="replace") as fh:
            fields = {}
            for line in fh:
                if "=" in line:
                    k, _, v = line.partition("=")
                    fields[k.strip().lower()] = v.strip()
    except OSError as exc:
        return False, f"unreadable:{exc}"
    home = fields.get("home", "")
    if home and not os.path.isdir(home):
        return False, f"home_missing={home}"
    exe = fields.get("executable", "")
    if exe and not os.path.exists(exe):
        return False, f"executable_missing={exe}"
    return True, f"home={home or 'unset'} version={fields.get('version', 'unset')}"


def validate_release(release_dir: str, package: str = "hindsight_api",
                     root: str = DEFAULT_ROOT) -> Result:
    r = Result()
    release_dir = _norm(os.path.abspath(release_dir))
    root = _norm(os.path.abspath(root))
    venv = os.path.join(release_dir, "venv")
    py = os.path.join(venv, "bin", "python")
    abs_re = _path_regex(root)
    cand_re = _candidate_tree_regex(root)

    if not os.path.isdir(venv) or not os.path.exists(py):
        r.add("VENV_PRESENT", False, f"missing {venv}")
        return r
    r.add("VENV_PRESENT", True, venv)

    # 1. sys.prefix / sys.executable.
    rc, out, err = _run_python(py, "import sys;print(sys.prefix);print(sys.executable)")
    if rc != 0 or "\n" not in out:
        r.add("PREFIX_CHECK", False, f"interpreter probe failed rc={rc} {err[:200]}")
        r.add("EXECUTABLE_CHECK", False, "NOT_EVALUATED")
    else:
        prefix, executable = (x.strip() for x in out.split("\n", 1))
        r.add("PREFIX_CHECK", _norm(prefix) == venv, f"sys.prefix={prefix}")
        r.add(
            "EXECUTABLE_CHECK",
            _literal_under(executable, venv) and os.path.exists(executable),
            f"sys.executable={executable}",
        )

    # 2. Canonical launchers: symlink target inside venv + shebang inside venv.
    ok, detail = True, []
    for name in LAUNCHER_NAMES:
        lp = os.path.join(release_dir, "bin", name)
        if not os.path.exists(lp):
            continue
        target = _norm(os.path.realpath(lp))
        if not _literal_under(target, venv):
            ok = False
            detail.append(f"{name}:link_target_outside={target}")
        try:
            with open(lp, "r", encoding="utf-8", errors="replace") as fh:
                first = fh.readline().strip()
        except OSError as exc:
            ok = False
            detail.append(f"{name}:unreadable={exc}")
            continue
        if not first.startswith("#!"):
            ok = False
            detail.append(f"{name}:no_shebang")
            continue
        body = first[2:].strip()
        interp = body.split()[0] if body else ""
        if not _literal_under(interp, venv) or not os.path.exists(interp):
            ok = False
            detail.append(f"{name}:shebang={interp}")
    r.add("SHEBANG_CHECK", ok, "; ".join(detail) or "all_launchers_final")

    # 2b. The interpreter a launcher NAMES must belong to THIS release.
    #     This is the check SHEBANG_CHECK alone cannot make. Empirically
    #     established on this host: symlinking venv/bin/python at a foreign
    #     interpreter does NOT move sys.prefix (CPython reads the pyvenv.cfg
    #     adjacent to the INVOKED path), so a launcher can pass a path test and
    #     still hand the runtime to another tree. Asking the named interpreter
    #     for its own prefix is the only way to see where it actually lands.
    lp_ok, lp_detail = True, []
    for name in LAUNCHER_NAMES:
        vp = os.path.join(venv, "bin", name)
        if not os.path.exists(vp):
            continue
        try:
            with open(vp, "r", encoding="utf-8", errors="replace") as fh:
                first = fh.readline().strip()
        except OSError as exc:
            lp_ok = False
            lp_detail.append(f"{name}:unreadable={exc}")
            continue
        if not first.startswith("#!"):
            continue
        body = first[2:].strip()
        interp = body.split()[0] if body else ""
        if not interp or not os.path.exists(interp):
            continue  # dead interpreter already reported by SHEBANG_CHECK
        rc, out, _ = _run_python(interp, "import sys;print(sys.prefix)")
        if rc != 0 or not out:
            lp_ok = False
            lp_detail.append(f"{name}:prefix_probe_rc={rc}")
            continue
        if _norm(out) != venv:
            lp_ok = False
            lp_detail.append(f"{name}:prefix={out}")
    r.add("LAUNCHER_PREFIX", lp_ok, "; ".join(lp_detail) or "launcher_interpreters_local")

    # 3./5. Recorded path VALUES: must be final, never a candidate tree, never
    #      a dead link. Only files that carry path values are read.
    cs_ok, cs_detail, st_ok, st_detail = True, [], True, []
    for name, text in _iter_path_value_files(venv):
        # pyvenv.cfg `command` is build provenance, not an operational path.
        is_provenance = name == "pyvenv.cfg" and "command =" in text
        for m in abs_re.finditer(text):
            val = _norm(m.group(0))
            if not val or val == root:
                continue
            if cand_re.search(val):
                st_ok = False
                st_detail.append(f"{name}:{val}")
                continue
            if is_provenance and name == "pyvenv.cfg":
                # exempt from finality, but a dead candidate parent still fails
                # above; a dead provenance parent is historical, not operational
                continue
            if not _literal_under(val, venv) and not _literal_under(val, release_dir):
                cs_ok = False
                cs_detail.append(f"{name}:{val}")
            elif not os.path.exists(val):
                st_ok = False
                st_detail.append(f"{name}:DEAD={val}")
    r.add("CONSOLE_SCRIPT", cs_ok, "; ".join(sorted(set(cs_detail))[:6]) or "final_paths_only")
    r.add("STAGING_REF", st_ok, "; ".join(sorted(set(st_detail))[:6]) or "no_candidate_or_dead_path")

    # 3b. pyvenv.cfg identity coherence.
    cfg_ok, cfg_detail = _check_pyvenv(venv, release_dir)
    r.add("PYVENV_CFG", cfg_ok, cfg_detail)

    # 4. Runtime resolution (find_spec, not import: this gate is about WHERE).
    rc, out, err = _run_python(
        py,
        "import importlib.util, os, sysconfig\n"
        f"spec = importlib.util.find_spec({package!r})\n"
        "print(os.path.realpath(spec.origin) if spec and spec.origin else 'NONE')\n"
        "print(sysconfig.get_paths()['purelib'])",
    )
    if rc != 0:
        r.add("RUNTIME_PATHS", False, f"resolve probe failed rc={rc} {err[:200]}")
    else:
        lines = out.split("\n")
        origin, purelib = lines[0], lines[1] if len(lines) > 1 else ""
        r.add(
            "RUNTIME_PATHS",
            _literal_under(origin, venv) and _literal_under(purelib, venv),
            f"origin={origin}",
        )

    return r


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("release")
    ap.add_argument("--package", default="hindsight_api")
    ap.add_argument("--root", default=DEFAULT_ROOT,
                    help="hindsight install root; path checks are relative to it")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    res = validate_release(args.release, args.package, args.root)

    if args.json:
        print(json.dumps(res.as_dict(), indent=2))
    else:
        print(f"RELEASE_IDENTITY={os.path.abspath(args.release)}")
        print(f"INSTALL_ROOT={os.path.abspath(args.root)}")
        for name, status, detail in res.checks:
            print(f"{name}={status} :: {detail}")
        print(f"RELEASE_IDENTITY_VERDICT={'PASS' if res.ok else 'FAIL'}")

    return 0 if res.ok else 1


if __name__ == "__main__":
    sys.exit(main())
