#!/usr/bin/env python3
"""Semantic desired-state reconciler for scoped WP-CLI environment isolation.

Recognizes the execution pipeline in tools/environments/{base,local,base_session_env}.py.
Detects PRE_NEEDS_REPAIR, POST_ALREADY_SATISFIED, AMBIGUOUS_BLOCKED.
Applies atomically; fail-closed on unknown layouts.
"""
from __future__ import annotations

import argparse
import ast
import os
import re
import textwrap
from pathlib import Path
from typing import Any

CUSTOM_ID = "CUSTOM_07"
STATE_ALREADY = "ALREADY_SATISFIED"
STATE_REIMPLEMENTED = "REIMPLEMENTED_PASS"
STATE_BLOCKED = "BLOCKED_UPSTREAM_INCOMPATIBLE"
STATE_FAIL = "FAIL_VALIDATION"

LOCAL = "tools/environments/local.py"
BASE = "tools/environments/base.py"
SESSION = "tools/environments/base_session_env.py"
REQUIRED = (LOCAL, BASE, SESSION)

# --- local.py helpers to insert ---
LOCAL_HELPER = r'''_WPCLI_BIN_NAMES = frozenset({"wp", "wp-cli", "wp.phar"})
_WPCLI_TRANSPARENT_WRAPPERS = frozenset({
    "command", "env", "exec", "nice", "nohup", "runuser", "setsid", "sudo", "su", "timeout",
})
_WPCLI_WRAPPER_VALUE_OPTIONS = {
    "env": frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}),
    "runuser": frozenset({"-u", "--user", "-g", "--group", "-G", "--supp-group", "-s", "--shell", "-c", "--command"}),
    "sudo": frozenset({"-u", "--user", "-g", "--group", "-C", "--close-from", "-p", "--prompt", "-r", "--role", "-t", "--type"}),
    "su": frozenset({"-s", "--shell", "-g", "--group", "-G", "--supp-group", "-c", "--command"}),
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
    "nice": frozenset({"-n", "--adjustment"}),
}


def _wpcli_command_name(token: str) -> bool:
    return os.path.basename(token).lower() in _WPCLI_BIN_NAMES


def _wpcli_command_at(tokens: list[str], index: int) -> bool:
    while index < len(tokens) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[index]):
        index += 1
    if index >= len(tokens):
        return False
    token = tokens[index]
    if _wpcli_command_name(token):
        return True
    wrapper = os.path.basename(token).lower()
    if wrapper not in _WPCLI_TRANSPARENT_WRAPPERS:
        return False
    index += 1
    options = _WPCLI_WRAPPER_VALUE_OPTIONS.get(wrapper, frozenset())
    while index < len(tokens):
        option = tokens[index]
        if option == "--":
            return _wpcli_command_at(tokens, index + 1)
        if option.startswith("-"):
            if wrapper == "command" and option in {"-v", "-V", "--help"}:
                return False
            option_name = option.split("=", 1)[0]
            index += 2 if option_name in options and "=" not in option else 1
            continue
        if wrapper == "env" and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", option):
            index += 1
            continue
        if wrapper == "timeout":
            return _wpcli_command_at(tokens, index + 1)
        return _wpcli_command_at(tokens, index)
    return False


def _is_wpcli_invocation(command: str) -> bool:
    try:
        import shlex
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>\n")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except (ValueError, TypeError):
        return False
    if not tokens or any(token in {";", "&&", "||", "|", "&", "(", ")", "<", ">"} for token in tokens):
        return False
    return _wpcli_command_at(tokens, 0)


def _prepend_git_bash_dirs(existing_path: str) -> str:
    """Keep Git and Bash discoverable after environment path scrubbing."""
    path_value = existing_path or os.defpath
    prefixes: list[str] = []
    for executable in ("git", "bash"):
        resolved = shutil.which(executable, path=path_value)
        if resolved:
            directory = os.path.dirname(resolved)
            if directory and directory not in prefixes:
                prefixes.append(directory)
    current = [entry for entry in path_value.split(os.pathsep) if entry]
    return os.pathsep.join(prefixes + [entry for entry in current if entry not in prefixes])


def build_wpcli_subprocess_env(base: Mapping[str, str]) -> dict[str, str]:
    """Copy base and remove LD_LIBRARY_PATH only for a WP-CLI child."""
    child_env = dict(base)
    child_env.pop("LD_LIBRARY_PATH", None)
    return child_env
'''

LOCAL_ENV_TEMPLATE = r'''def _make_run_env(env: dict, *, command: str | None = None, is_wpcli: bool | None = None) -> dict:
    """Build the normal run environment, then apply the WP-CLI-only child rule."""
    run_env = _scrubbed_env([(dict(os.environ | env), True)], frozenset(),
                            lambda p: _prepend_git_bash_dirs(_append_missing_sane_path_entries(p)))
    if is_wpcli is None:
        is_wpcli = bool(command and _is_wpcli_invocation(command))
    return build_wpcli_subprocess_env(run_env) if is_wpcli else run_env
'''

SESSION_TEMPLATE = r'''def _wrap_command_script(
    command: str, *, quoted_cwd: str, quoted_snap: str, snap_tmp_template: str,
    passthrough_names: Iterable[str], snapshot_ready: bool, cwd_marker: str,
    wpcli_invocation: bool = False) -> str:
    """Source the snapshot, run one command, and atomically refresh the snapshot.

    WP-CLI is classified from the original command by BaseEnvironment. The
    snapshot is allowed to restore ordinary shell state, but LD_LIBRARY_PATH
    is removed after that source and restored only before the redump.
    """
    escaped = command.replace("'", "'\\''")
    save, restore = _passthrough_save_restore(passthrough_names)
    parts = list(save)
    if snapshot_ready:
        parts.append(f"source {quoted_snap} >/dev/null 2>&1 || true")
    if wpcli_invocation:
        parts += [
            '__hermes_wpcli_ld_present="${LD_LIBRARY_PATH+x}"',
            '__hermes_wpcli_ld_value="${LD_LIBRARY_PATH-}"',
            "unset LD_LIBRARY_PATH",
        ]
    parts += restore
    parts += [
        'export AI_AGENT="${AI_AGENT:-hermes-agent}" HERMES_AGENT="${HERMES_AGENT:-true}"',
        'export GIT_PAGER="${GIT_PAGER:-cat}" PAGER="${PAGER:-cat}"',
        f"builtin cd -- {quoted_cwd} || exit 126",
        f"eval '{escaped}'",
        "__hermes_ec=$?",
        "umask 077"]
    if wpcli_invocation:
        parts.insert(-1, (
            'if [ "$__hermes_wpcli_ld_present" = x ]; then '
            'export LD_LIBRARY_PATH="$__hermes_wpcli_ld_value"; '
            'else unset LD_LIBRARY_PATH; fi'))
    if snapshot_ready:
        parts.append(
            f"__hermes_snap_tmp=$(mktemp {snap_tmp_template}) && "
            f"{{ {_export_dump_excluding_session_vars(_SNAP_TMP, passthrough_names)} "
            f"&& mv -f {_SNAP_TMP} {quoted_snap}; }} "
            f"2>/dev/null || rm -f {_SNAP_TMP} 2>/dev/null || true")
    parts += [_cwd_marker_printf(cwd_marker), "exit $__hermes_ec"]
    return "\n".join(parts)
'''

BASE_CLASSIFIER = r'''    def _classify_command_for_child_env(self, command: str) -> bool:
        """Backends other than LocalEnvironment do not change child env globally."""
        return False

'''
LOCAL_CLASSIFIER = r'''    def _classify_command_for_child_env(self, command: str) -> bool:
        """Classify the caller command before BaseEnvironment wraps it."""
        return _is_wpcli_invocation(command)

'''


def read(repo: Path, rel: str) -> str:
    path = repo / rel
    if not path.is_file():
        raise RuntimeError(f"{STATE_BLOCKED}: missing {rel}")
    return path.read_text(encoding="utf-8")


def funcs(source: str, rel: str) -> dict[str, ast.AST]:
    try:
        tree = ast.parse(source, filename=rel)
    except SyntaxError as exc:
        raise RuntimeError(f"{STATE_BLOCKED}: AST parse failed {rel}: {exc}") from exc
    found: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name not in found:
                found[node.name] = node
    return found


def function_source(source: str, node: ast.AST) -> str:
    return ast.get_source_segment(source, node) or ""


def replace_function(source: str, node: ast.AST, replacement: str) -> str:
    lines = source.splitlines(keepends=True)
    start = node.lineno - 1
    end = node.end_lineno
    indent = lines[start][: len(lines[start]) - len(lines[start].lstrip())]
    normalized = textwrap.dedent(replacement).strip("\n")
    body = "\n".join(indent + line if line else "" for line in normalized.splitlines()) + "\n"
    return "".join(lines[:start]) + body + "".join(lines[end:])


def inspect_contract(repo: Path) -> dict[str, Any]:
    local = read(repo, LOCAL)
    base = read(repo, BASE)
    session = read(repo, SESSION)
    lf = funcs(local, LOCAL)
    bf = funcs(base, BASE)
    sf = funcs(session, SESSION)
    local_wp = lf.get("_is_wpcli_invocation")
    local_env = lf.get("build_wpcli_subprocess_env")
    make_env = lf.get("_make_run_env")
    run_bash = lf.get("_run_bash")
    execute = bf.get("execute")
    wrap = bf.get("_wrap_command")
    session_wrap = sf.get("_wrap_command_script")
    local_text = function_source(local, local_wp) if local_wp else ""
    env_text = function_source(local, local_env) if local_env else ""
    make_text = function_source(local, make_env) if make_env else ""
    run_text = function_source(local, run_bash) if run_bash else ""
    exec_text = function_source(base, execute) if execute else ""
    wrap_text = function_source(base, wrap) if wrap else ""
    session_text = function_source(session, session_wrap) if session_wrap else ""
    return {
        "local_source": local, "base_source": base, "session_source": session,
        "local_functions": lf, "base_functions": bf, "session_functions": sf,
        "classification": bool(local_wp and "runuser" in local and "_wpcli_command_at" in local and "punctuation_chars" in local),
        "true_detection": bool(local_wp and "_wpcli_command_name" in local and "return _wpcli_command_at(tokens, 0)" in local),
        "child_copy_pop": bool(local_env and "child_env = dict(base)" in env_text and 'child_env.pop("LD_LIBRARY_PATH", None)' in env_text),
        "make_env_authoritative": bool(make_env and "is_wpcli" in make_text and "build_wpcli_subprocess_env" in make_text),
        "run_bash_transport": bool(run_bash and "is_wpcli" in run_text and "_make_run_env(self.env, is_wpcli=is_wpcli)" in run_text),
        "original_classification": bool(execute and "_classify_command_for_child_env(" in exec_text and "original_command = command" in exec_text),
        "wrap_transport": bool(wrap and "wpcli_invocation=wpcli_invocation" in wrap_text and "wpcli_invocation=is_wpcli" in exec_text),
        "local_spawn_transport": bool(execute and "is_local" in exec_text and "is_wpcli=is_wpcli" in exec_text),
        "snapshot_final_unset": bool(session_wrap and "wpcli_invocation" in session_text and "unset LD_LIBRARY_PATH" in session_text and "eval '" in session_text),
        "snapshot_restore_before_redump": bool(session_wrap and "__hermes_wpcli_ld_value" in session_text and "_export_dump_excluding_session_vars" in session_text),
    }


def satisfied(state: dict[str, Any]) -> bool:
    keys = ("classification", "true_detection", "child_copy_pop", "make_env_authoritative", "run_bash_transport",
            "original_classification", "wrap_transport", "local_spawn_transport", "snapshot_final_unset",
            "snapshot_restore_before_redump")
    return all(state.get(key) for key in keys)


def _apply_local(local: str) -> tuple[str, bool]:
    """Apply WP-CLI changes to local.py. Returns (new_source, changed)."""
    changed = False
    lf = funcs(local, LOCAL)

    # 1. Insert helpers before _make_run_env if not present
    if "_is_wpcli_invocation" not in lf:
        anchor = "def _make_run_env("
        if anchor not in local:
            raise RuntimeError(f"{STATE_BLOCKED}: _make_run_env anchor missing in local.py")
        local = local.replace(anchor, LOCAL_HELPER + "\n\n" + anchor, 1)
        changed = True
        lf = funcs(local, LOCAL)

    # 2. Replace _make_run_env with the new signature
    make = lf.get("_make_run_env")
    if make is None:
        raise RuntimeError(f"{STATE_BLOCKED}: _make_run_env missing after helper insertion")
    make_text = function_source(local, make)
    if "is_wpcli" not in make_text:
        local = replace_function(local, make, LOCAL_ENV_TEMPLATE)
        changed = True
        lf = funcs(local, LOCAL)

    # 3. Add is_wpcli to _run_bash signature
    run = lf.get("_run_bash")
    if run is None:
        raise RuntimeError(f"{STATE_BLOCKED}: _run_bash missing")
    run_text = function_source(local, run)
    new_run = textwrap.dedent("    " + re.sub(
        r"def _run_bash\((?P<args>[^)]*stdin_data: str \| None = None)\) -> subprocess\.Popen:",
        r"def _run_bash(\g<args>, is_wpcli: bool | None = None) -> subprocess.Popen:",
        run_text, count=1))
    if new_run != run_text and "is_wpcli: bool | None" not in run_text:
        local = replace_function(local, run, new_run)
        changed = True
        lf = funcs(local, LOCAL)
        run_text = new_run
    # 4. Update Popen env call
    if "env=_make_run_env(self.env)," in local:
        local = local.replace("env=_make_run_env(self.env),", "env=_make_run_env(self.env, is_wpcli=is_wpcli),", 1)
        changed = True
    elif "env=_make_run_env(self.env, is_wpcli=is_wpcli)," not in local:
        raise RuntimeError(f"{STATE_BLOCKED}: _run_bash Popen env anchor missing")

    # 5. Add _classify_command_for_child_env to LocalEnvironment
    if "def _classify_command_for_child_env" not in local:
        anchor = "    is_local = True\n\n"
        if anchor not in local:
            raise RuntimeError(f"{STATE_BLOCKED}: LocalEnvironment is_local anchor missing")
        local = local.replace(anchor, anchor + LOCAL_CLASSIFIER, 1)
        changed = True

    return local, changed


def _apply_base(base: str) -> tuple[str, bool]:
    """Apply WP-CLI changes to base.py. Returns (new_source, changed)."""
    changed = False

    # 1. Add _classify_command_for_child_env before _wrap_command
    if "def _classify_command_for_child_env" not in base:
        anchor = "    def _wrap_command("
        if anchor not in base:
            raise RuntimeError(f"{STATE_BLOCKED}: BaseEnvironment _wrap_command anchor missing")
        base = base.replace(anchor, BASE_CLASSIFIER + anchor, 1)
        changed = True

    # 2. Add wpcli_invocation to _wrap_command signature
    old_sig = "def _wrap_command(self, command: str, cwd: str) -> str:"
    new_sig = "def _wrap_command(self, command: str, cwd: str, *, wpcli_invocation: bool = False) -> str:"
    if old_sig in base:
        base = base.replace(old_sig, new_sig, 1)
        changed = True
    elif new_sig not in base:
        raise RuntimeError(f"{STATE_BLOCKED}: _wrap_command signature shape changed")

    # 3. Add wpcli_invocation to _wrap_command_script call
    if "wpcli_invocation=wpcli_invocation" not in base:
        old_call = "            **self._snapshot_script_kwargs(cwd))"
        new_call = "            wpcli_invocation=wpcli_invocation,\n            **self._snapshot_script_kwargs(cwd))"
        if old_call in base:
            base = base.replace(old_call, new_call, 1)
            changed = True
        else:
            raise RuntimeError(f"{STATE_BLOCKED}: _wrap_command_script call anchor missing")

    # 4. Add original_command / is_wpcli in execute()
    if "original_command = command" not in base:
        old_exec = "        self._before_execute()\n\n        exec_command, sudo_stdin"
        new_exec = "        self._before_execute()\n\n        original_command = command\n        is_wpcli = self._classify_command_for_child_env(original_command)\n        exec_command, sudo_stdin"
        if old_exec in base:
            base = base.replace(old_exec, new_exec, 1)
            changed = True
        else:
            raise RuntimeError(f"{STATE_BLOCKED}: execute() _before_execute anchor missing")

    # 5. Pass wpcli_invocation to _wrap_command
    old_wrap = "        wrapped = self._wrap_command(exec_command, effective_cwd)\n"
    new_wrap = "        wrapped = self._wrap_command(exec_command, effective_cwd, wpcli_invocation=is_wpcli)\n"
    if old_wrap in base:
        base = base.replace(old_wrap, new_wrap, 1)
        changed = True
    elif new_wrap not in base:
        raise RuntimeError(f"{STATE_BLOCKED}: _wrap_command call anchor missing")

    # 6. Conditionally pass is_wpcli to _run_bash in _spawn_and_wait
    if "is_wpcli=is_wpcli" not in base:
        # Find the _run_bash call inside _spawn_and_wait — handle both
        # 12-space (old) and 16-space (new, inside try/finally) indentation.
        pat = re.compile(
            r"(?P<indent>\s+)spawned = self\._run_bash\(wrapped, login=login, "
            r"timeout=effective_timeout, stdin_data=effective_stdin\)\n")
        m = pat.search(base)
        if m is None:
            raise RuntimeError(f"{STATE_BLOCKED}: _run_bash call inside _spawn_and_wait not found")
        indent = m.group("indent")
        inner = indent + "    "
        new_spawn = (
            f"{indent}if self.is_local:\n"
            f"{inner}spawned = self._run_bash(wrapped, login=login, timeout=effective_timeout,\n"
            f"{inner}                                    stdin_data=effective_stdin, is_wpcli=is_wpcli)\n"
            f"{indent}else:\n"
            f"{inner}spawned = self._run_bash(wrapped, login=login, timeout=effective_timeout,\n"
            f"{inner}                                    stdin_data=effective_stdin)\n"
        )
        base = base[:m.start()] + new_spawn + base[m.end():]
        changed = True

    return base, changed


def _apply_session(session: str) -> tuple[str, bool]:
    """Apply WP-CLI changes to base_session_env.py."""
    sf = funcs(session, SESSION)
    session_func = sf.get("_wrap_command_script")
    if session_func is None:
        raise RuntimeError(f"{STATE_BLOCKED}: snapshot wrapper missing")
    session_text = function_source(session, session_func)
    if "wpcli_invocation" in session_text and "unset LD_LIBRARY_PATH" in session_text:
        return session, False
    new_session = replace_function(session, session_func, SESSION_TEMPLATE)
    return new_session, True


def apply(repo: Path) -> tuple[str, list[str]]:
    state = inspect_contract(repo)
    if satisfied(state):
        return STATE_ALREADY, []

    local, local_changed = _apply_local(state["local_source"])
    base, base_changed = _apply_base(state["base_source"])
    session, session_changed = _apply_session(state["session_source"])

    changed = []
    if local_changed:
        changed.append(LOCAL)
    if base_changed:
        changed.append(BASE)
    if session_changed:
        changed.append(SESSION)

    if not changed:
        # All individual apply functions said no change, but overall not satisfied.
        raise RuntimeError(f"{STATE_BLOCKED}: cannot determine how to reach POST state; "
                           f"contract incomplete but no individual patch applied")

    # Validate all changed sources parse before writing.
    for rel, source in ((LOCAL, local), (BASE, base), (SESSION, session)):
        try:
            ast.parse(source, filename=rel)
        except SyntaxError as exc:
            raise RuntimeError(f"{STATE_FAIL}: generated {rel} does not parse: {exc}") from exc

    # Write atomically (in-place; caller has backup)
    (repo / LOCAL).write_text(local, encoding="utf-8")
    (repo / BASE).write_text(base, encoding="utf-8")
    (repo / SESSION).write_text(session, encoding="utf-8")

    final = inspect_contract(repo)
    if not satisfied(final):
        raise RuntimeError(f"{STATE_FAIL}: semantic contract incomplete after apply")
    return STATE_REIMPLEMENTED, changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--repo", required=True)
    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()
    try:
        state = inspect_contract(repo)
        keys = ("classification", "true_detection", "child_copy_pop", "make_env_authoritative",
                "run_bash_transport", "original_classification", "wrap_transport",
                "local_spawn_transport", "snapshot_final_unset", "snapshot_restore_before_redump")
        missing = [key for key in keys if not state.get(key)]
        if args.check:
            print(f"WPCLI_CUSTOM_ID={CUSTOM_ID}")
            print(f"WPCLI_CONTRACT={'PASS' if not missing else 'NEEDS_REIMPLEMENTATION'}")
            print(f"WPCLI_MISSING={','.join(missing) if missing else 'NONE'}")
            for key in keys:
                print(f"WPCLI_{key.upper()}={'PASS' if state.get(key) else 'FAIL'}")
            if not missing:
                print(f"WPCLI_CUSTOM_RESULT={STATE_ALREADY}")
                return 0
            print(f"WPCLI_CUSTOM_RESULT={STATE_REIMPLEMENTED}")
            return 1
        result, changed = apply(repo)
        print(f"WPCLI_CUSTOM_ID={CUSTOM_ID}")
        print(f"WPCLI_CUSTOM_RESULT={result}")
        print(f"WPCLI_CHANGED_FILES={','.join(changed) if changed else 'NONE'}")
        return 0
    except RuntimeError as exc:
        message = str(exc)
        code = STATE_BLOCKED if message.startswith(STATE_BLOCKED) else STATE_FAIL
        print(f"WPCLI_CUSTOM_ID={CUSTOM_ID}")
        print(f"WPCLI_CUSTOM_RESULT={code}")
        print(f"WPCLI_CUSTOM_ERROR={message}")
        return 2
    except Exception as exc:
        print(f"WPCLI_CUSTOM_ID={CUSTOM_ID}")
        print(f"WPCLI_CUSTOM_RESULT={STATE_FAIL}")
        print(f"WPCLI_CUSTOM_ERROR={type(exc).__name__}: {exc}")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
