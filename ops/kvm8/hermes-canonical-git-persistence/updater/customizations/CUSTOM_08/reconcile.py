#!/usr/bin/env python3
"""CUSTOM_08 — Lifecycle PM normalizado dos wrappers de perfil em ~/.local/bin.

Problema (observado em 2026-09-26 no KVM8): os wrappers de perfil em
/root/.local/bin/hermes-* divergiam do lifecycle PM oficial.

Upstream (hermes_cli/_launchers.py::_publish_conveniences + profiles.py::
create_wrapper_script) publica apenas {hermes, hermes-agent, hermes-acp} e escreve
aliases de perfil como `exec hermes -p <profile> "$@"` (classe GENERIC_PM). Ele NÃO
reescreve wrappers de perfil já existentes fora desse conjunto, mas o mecanismo
`_owns_launcher` considera "nosso" qualquer wrapper que mencione
<install_root>/.hermes/bin/hermes — logo um wrapper PM_DIRECT também é republished
para o template genérico a cada expose_cli/update.

Contrato desejado (KVM8, fail-closed):
A) Todo wrapper operacional de perfil executa o PM launcher canônico
   <install_root>/.hermes/bin/hermes — nunca /root/.hermes/venvs/**/bin/hermes,
   nunca .venv/bin/hermes.
B) Todo wrapper de perfil preserva PROFILE e exporta
   HERMES_HOME=/root/.hermes/profiles/${PROFILE} (isolamento por perfil).
C) O flag -p usa o LITERAL do profile, não a variável "$PROFILE": o doctor
   upstream (hermes_cli/doctor_state.py, regex `hermes -p (\\S+)`) lê o wrapper
   como texto; com "$PROFILE" ele casa a string literal `"$PROFILE"`, resolve
   profile_exists('"$PROFILE"') == False e emite "Orphan alias" para todo wrapper.
D) Nenhum wrapper pode apontar HERMES_HOME para a raiz genérica /root/.hermes.
E) HERMES_HOME de um profile NUNCA pode ser o home de outro profile.
F) O runtime efetivo deve ser o Python do PM (3.14.7 na linha 0.21.5), nunca o
   Python 3.11.x do venv legado hermes-kvm8-canonical.

Isto NÃO altera provider/model/auth, não toca config.yaml de profile, não
reinicia gateway e não reativa timers.
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ─── Constants ───────────────────────────────────────────────────────────────
WRAPPER_DIR = Path("/root/.local/bin")
GENERIC = "hermes"
# {alias: profile} — derivando do conteudo do wrapper (PROFILE="..."), nunca do
# basename sozinho. Aliases sem PROFILE propio sao launchers de runtime.
RUNTIME_LAUNCHERS = frozenset({"hermes-agent"})
LEGACY_VENV_RE = re.compile(r"/root/\.hermes/venvs/[^\s'\"]*/bin/hermes")
LOCAL_VENV_RE = re.compile(r"\.venv/bin/hermes")
PROFILE_RE = re.compile(r'^PROFILE="([^"]+)"', re.MULTILINE)
HERMES_HOME_RE = re.compile(r'export HERMES_HOME="([^"]+)"')
ANY_P_RE = re.compile(r"hermes\s+-p\s+(\S+)")


class ReconcileError(RuntimeError):
    """Erro que DEVE bloquear o pipeline — nunca tratado como PASS."""

    def __init__(self, message: str, code: str = "BLOCKED_WRAPPER_STATE") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class WrapperState:
    name: str
    path: Path
    profile: Optional[str]
    hermes_home: Optional[str]
    pm_direct: bool
    legacy_venv: bool
    local_venv: bool
    p_literal: Optional[str]
    generic_root_home: bool
    readable: bool
    text: str

    @property
    def home_owner(self) -> Optional[str]:
        if not self.hermes_home:
            return None
        return Path(self.hermes_home).name if self.hermes_home.rstrip("/").endswith(
            tuple(self.profile or "")) or "/profiles/" in self.hermes_home else None


def _install_root(repo: Path) -> Path:
    """install_root do PM: o repo alvo, salvo override explicito."""
    override = os.environ.get("HERMES_INSTALL_ROOT")
    return Path(override).resolve() if override else repo.resolve()


def _pm_launcher(install_root: Path) -> Path:
    return install_root / ".hermes" / "bin" / "hermes"


def discover_wrappers() -> List[Path]:
    """Enumera /root/.local/bin/hermes e hermes-* — nunca uma lista fixa de nomes."""
    if not WRAPPER_DIR.is_dir():
        return []
    found = {p for p in WRAPPER_DIR.iterdir() if p.is_file() and not p.is_symlink()}
    generic = WRAPPER_DIR / GENERIC
    if generic.is_file():
        found.add(generic)
    return sorted(found, key=lambda p: p.name)


def _is_rollback_artifact(path: Path) -> bool:
    """Artefatos de backup/probe desta tarefa nao sao wrappers operacionais.

    Cobre ``<name>.pre-pm-<UTC>`` e variantes de probe (``.durability-probe``,
    ``.probe``), que nao correspondem a nenhum profile e nao devem ser alvo de
    escrita nem entrar no gate.
    """
    return bool(
        re.search(r"\.(pre-pm-\d{8}T\d{6}Z|pre-pm-\w+|durability-probe|probe)$", path.name)
    )


def inspect(path: Path, install_root: Path) -> WrapperState:
    try:
        text = path.read_text(encoding="utf-8-sig")
        readable = True
    except (OSError, UnicodeError):
        # A binario (ex.: o `uv` de 59 MB em ~/.local/bin) convive com wrappers no
        # mesmo diretorio. Um wrapper ilegivel e FAIL, nunca um traceback.
        text, readable = "", False

    pm = str(_pm_launcher(install_root))
    profile_m = PROFILE_RE.search(text)
    home_m = HERMES_HOME_RE.search(text)
    p_m = ANY_P_RE.search(text)

    return WrapperState(
        name=path.name,
        path=path,
        profile=profile_m.group(1) if profile_m else None,
        hermes_home=home_m.group(1) if home_m else None,
        pm_direct=pm in text,
        legacy_venv=bool(LEGACY_VENV_RE.search(text)),
        local_venv=bool(LOCAL_VENV_RE.search(text)),
        p_literal=p_m.group(1) if p_m else None,
        generic_root_home=bool(
            home_m and home_m.group(1).rstrip("/") == "/root/.hermes"
        ),
        readable=readable,
        text=text,
    )


def profile_wrappers(states: List[WrapperState]) -> List[WrapperState]:
    """Wrappers de perfil = tem PROFILE= e nao e backup nem runtime launcher puro."""
    out = []
    for s in states:
        if _is_rollback_artifact(s.path):
            continue
        if s.name in RUNTIME_LAUNCHERS:
            continue
        if not s.profile:
            continue
        out.append(s)
    return out


def evaluate(states: List[WrapperState], install_root: Path) -> List[str]:
    """Retorna lista de violacoes (vazia = desired state satisfeito)."""
    problems: List[str] = []
    homes: Dict[str, str] = {}

    for s in profile_wrappers(states):
        tag = f"wrapper={s.name}"

        if not s.readable:
            problems.append(f"{tag} FAIL=UNREADABLE")
            continue

        # A) exec target must be the canonical PM launcher
        if not s.pm_direct:
            if s.legacy_venv:
                problems.append(f"{tag} FAIL=LEGACY_VENV_EXEC (proibido em wrapper operacional)")
            elif s.local_venv:
                problems.append(f"{tag} FAIL=LOCAL_VENV_EXEC (proibido em wrapper operacional)")
            else:
                problems.append(f"{tag} FAIL=NOT_PM_DIRECT (exec target nao e o PM launcher)")

        # B) profile home isolation
        if not s.hermes_home:
            problems.append(f"{tag} FAIL=NO_HERMES_HOME_EXPORT")
        else:
            # O wrapper exporta a forma com ${PROFILE}; o shell expande em runtime.
            # expanding ANTES de validar evita classificar o template como divergencia.
            resolved_home = s.hermes_home.replace("${PROFILE}", s.profile or "")
            if "${" in resolved_home:
                problems.append(
                    f"{tag} FAIL=UNRESOLVED_HERMES_HOME_VAR (got={s.hermes_home})"
                )
            expected_suffix = f"/profiles/{s.profile}"
            if not resolved_home.rstrip("/").endswith(expected_suffix):
                problems.append(
                    f"{tag} FAIL=WRONG_HERMES_HOME "
                    f"(got={resolved_home} expected_suffix={expected_suffix})"
                )
            owner = resolved_home.rstrip("/").rsplit("/", 1)[-1]
            if owner in homes and homes[owner] != s.name:
                # Varios aliases podem apontar legitimamente para o MESMO profile
                # (ex.: hermes-statusbar -> hermes-kvm8; `hermes profile list`
                # reporta hermes-statusbar como o alias ativo de hermes-kvm8).
                # O que e proibido e home compartilhado entre profiles DISTINTOS.
                if homes[owner] != owner:
                    problems.append(
                        f"{tag} FAIL=SHARED_PROFILE_HOME com {homes[owner]} "
                        f"(home={owner} pertence a outro profile)"
                    )
                else:
                    print(f"  · {s.name}: alias legítimo do profile {owner}")
            homes.setdefault(owner, s.name if owner == s.name else owner)
            if not Path(resolved_home).is_dir():
                problems.append(f"{tag} FAIL=PROFILE_HOME_MISSING ({resolved_home})")

        # C) -p must carry the literal profile name (doctor orphan-alias contract)
        if s.p_literal is None:
            problems.append(f"{tag} FAIL=NO_MINUS_P (perfil nao fixado explicitamente)")
        elif s.p_literal.startswith('"') or s.p_literal.startswith("$") or s.p_literal == "${PROFILE}":
            problems.append(
                f"{tag} FAIL=NON_LITERAL_MINUS_P (got={s.p_literal!r}; "
                "doctor regex 'hermes -p (\\S+)' resolve isso como profile inexistente)"
            )
        elif s.p_literal != s.profile:
            problems.append(
                f"{tag} FAIL=MINUS_P_PROFILE_MISMATCH "
                f"(-p {s.p_literal} != PROFILE={s.profile})"
            )

        # D) never the generic hermes root
        if s.generic_root_home:
            problems.append(f"{tag} FAIL=GENERIC_ROOT_HERMES_HOME (/root/.hermes)")

    return problems


def render(s: WrapperState) -> str:
    if s.profile:
        return (
            "#!/usr/bin/env bash\n"
            f'PROFILE="{s.profile}"\n'
            'export HERMES_HOME="/root/.hermes/profiles/${PROFILE}"\n'
            "exec {pm} -p {profile} \"$@\"\n"
        ).format(pm="PM_LAUNCHER", profile=s.profile)
    return s.text


def desired_content(s: WrapperState, install_root: Path) -> str:
    """Conteudo PM_DIRECT desejado para um wrapper de perfil.

    Preserva o shebang original para nao mudar o interpretador do launcher.
    """
    shebang = s.text.splitlines()[0] if s.text.strip() else "#!/usr/bin/env bash"
    if not shebang.startswith("#!"):
        shebang = "#!/usr/bin/env bash"
    return (
        f"{shebang}\n"
        f'PROFILE="{s.profile}"\n'
        'export HERMES_HOME="/root/.hermes/profiles/${PROFILE}"\n'
        f'exec {_pm_launcher(install_root)} -p {s.profile} "$@"\n'
    )


def backup_path(path: Path, stamp: str) -> Path:
    return path.with_name(f"{path.name}.pre-pm-{stamp}")


def apply(states: List[WrapperState], install_root: Path, stamp: str) -> int:
    changed = 0
    for s in profile_wrappers(states):
        wanted = desired_content(s, install_root)
        if s.text == wanted:
            print(f"  ✓ {s.name}: já em PM_DIRECT literal")
            continue
        bak = backup_path(s.path, stamp)
        if not bak.exists():
            subprocess.run(["cp", "-a", str(s.path), str(bak)], check=True)
            print(f"  · backup {bak}")
        s.path.write_text(wanted, encoding="utf-8")
        subprocess.run(["chmod", "+x", str(s.path)], check=True)
        print(f"  → {s.name}: normalizado para PM_DIRECT literal")
        changed += 1
    return changed


def runtime_python_version(install_root: Path) -> Tuple[bool, str]:
    """F6: o launcher efetivo deve rodar no Python do PM, nao no 3.11 legado."""
    launcher = _pm_launcher(install_root)
    if not launcher.is_file():
        return False, "PM_LAUNCHER_MISSING"
    try:
        proc = subprocess.run(
            [str(launcher), "--version"],
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        return False, "VERSION_TIMEOUT"
    out = f"{proc.stdout}\n{proc.stderr}"
    if proc.returncode != 0:
        return False, f"VERSION_RC={proc.returncode}"
    if "3.11" in out:
        return False, "LEGACY_PYTHON_3_11_IN_RUNTIME"
    return True, out.strip().splitlines()[0] if out.strip() else "OK"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--repo", required=True, help="install root alvo (repo do Hermes)")
    args = ap.parse_args()

    if args.check == args.apply:
        print("CUSTOM_08_ERROR=usage: --check | --apply (exatamente um)", file=sys.stderr)
        return 2

    repo = Path(args.repo).resolve()
    install_root = _install_root(repo)
    states = [inspect(p, install_root) for p in discover_wrappers()]

    targets = profile_wrappers(states)
    print(f"CUSTOM_08_PM_LAUNCHER={_pm_launcher(install_root)}")
    print(f"CUSTOM_08_PROFILE_WRAPPERS={len(targets)}")
    for s in targets:
        print(f"  - {s.name}: profile={s.profile} home={s.hermes_home} "
              f"pm_direct={s.pm_direct} minus_p={s.p_literal} "
              f"legacy={s.legacy_venv} generic_root={s.generic_root_home}")

    problems = evaluate(states, install_root)
    ok_runtime, detail = runtime_python_version(install_root)
    if not ok_runtime:
        problems.append(f"RUNTIME_PYTHON FAIL={detail}")
    print(f"CUSTOM_08_RUNTIME_PYTHON={'OK' if ok_runtime else 'FAIL'} ({detail})")

    if args.check:
        if problems:
            print(f"CUSTOM_08_RESULT=FAIL_VALIDATION ({len(problems)} violation(s))")
            for p in problems:
                print(f"  ! {p}")
            return 1
        print("CUSTOM_08_RESULT=PASS")
        return 0

    changed = apply(states, install_root, stamp=os.environ.get("CUSTOM_08_STAMP", "manual"))
    if not changed:
        print("CUSTOM_08_RESULT=PASS_EXISTING (nenhum wrapper precisou de mudanca)")
        return 0

    # reavaliar apos aplicar
    states2 = [inspect(p, install_root) for p in discover_wrappers()]
    remaining = evaluate(states2, install_root)
    if remaining:
        print(f"CUSTOM_08_RESULT=FAIL_POST_APPLY ({len(remaining)} violation(s))")
        for p in remaining:
            print(f"  ! {p}")
        return 1
    print(f"CUSTOM_08_RESULT=PASS ({changed} wrapper(s) normalizado(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
