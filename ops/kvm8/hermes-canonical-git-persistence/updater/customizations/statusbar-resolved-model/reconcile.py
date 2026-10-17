#!/usr/bin/env python3
"""Reconciliador externo do statusbar; reconstrói arquiteturas A/B no repo fornecido."""
from __future__ import annotations

import argparse
import ast
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

REPO = os.environ.get("HERMES_CANONICAL_REPO", "/opt/hermes-agent-clean.release-0.22.2-hermes-kvm8-0222-20261016T005835Z")


def read_file(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


def write_file(path: str, source: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(source)


def parse_ast(source: str, path: str = "<unknown>") -> Tuple[Optional[ast.AST], Optional[str]]:
    try:
        return ast.parse(source), None
    except SyntaxError as exc:
        return None, f"AST_PARSE_FAIL: {path}: {exc}"


def find_function(tree: ast.AST, name: str) -> Optional[ast.AST]:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def find_normalize_model_response(tree: ast.AST) -> Optional[ast.AST]:
    return find_function(tree, "normalize_model_response")


def find_get_status_bar_snapshot(tree: ast.AST) -> Optional[ast.AST]:
    return find_function(tree, "_get_status_bar_snapshot")


def response_is_in_scope(func_node: ast.AST) -> Tuple[bool, str]:
    args = func_node.args  # type: ignore[attr-defined]
    names = {arg.arg for arg in args.args + args.posonlyargs + args.kwonlyargs}
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)
    ok = "response" in names
    return ok, "RESPONSE_ARG_BOUND" if ok else "RESPONSE_NOT_BOUND_IN_SCOPE"


def is_response_model_getattr(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "response"
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value == "model"
    )


def has_response_model_getattr(tree: ast.AST) -> bool:
    return any(is_response_model_getattr(node) for node in ast.walk(tree))


def assignment_to_last_actual(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Assign):
            for target in child.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "agent"
                    and target.attr == "_last_actual_response_model"
                ):
                    return True
    return False


def detect_strategy(tree: ast.AST) -> str:
    func = find_normalize_model_response(tree)
    if func is None:
        return "NONE"
    has_getattr = has_response_model_getattr(tree)
    has_assign = assignment_to_last_actual(func)
    if has_assign and has_getattr:
        return "HYBRID"
    if has_assign:
        return "A_GOLDEN"
    if has_getattr:
        return "B_TARGET"
    return "UNDEFINED"


def has_response_model_read(func_node: ast.AST) -> bool:
    return any(is_response_model_getattr(node) for node in ast.walk(func_node))


def has_nonempty_guard(func_node: ast.AST) -> bool:
    # The reconstructed block must normalize and reject empty model values.
    source = ast.unparse(func_node) if hasattr(ast, "unparse") else ast.dump(func_node)
    return ".strip()" in source and "actual_response_model" in source


def has_snapshot_last_actual(snapshot_func: ast.AST) -> bool:
    for node in ast.walk(snapshot_func):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "agent"
            and node.attr == "_last_actual_response_model"
        ):
            return True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "agent"
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "_last_actual_response_model"
        ):
            return True
    return False


def _is_getattr_attr(call: ast.AST, obj_name: str, attr: str) -> bool:
    return (
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "getattr"
        and len(call.args) >= 2
        and isinstance(call.args[0], ast.Name)
        and call.args[0].id == obj_name
        and isinstance(call.args[1], ast.Constant)
        and call.args[1].value == attr
    )


def _bool_or_operand_text(op: ast.AST) -> str:
    try:
        return ast.unparse(op) if hasattr(ast, "unparse") else ""
    except Exception:  # pragma: no cover - defensive only
        return ""


def is_model_resolution_expression(value: ast.AST) -> bool:
    """True when `value` is the fallback chain that resolves the status-bar model.

    The upstream terminal fallback is NOT stable text: it moved from the bare
    literal "unknown" to the i18n call `t("cli.shared.unknown")`. Matching that
    string is what broke acceptance on newer targets, so the contract is matched
    structurally instead: a BoolOp chain whose FIRST operand reads agent.model
    and whose SECOND operand is self.model, terminated by a real fallback term
    (a Constant or a Call). A chain that merely trails off into another model
    source such as a bare `self.model` is truncated, not a valid anchor, and
    must fail closed rather than be treated as reconstructible.
    """
    if not isinstance(value, ast.BoolOp) or not isinstance(value.op, ast.Or):
        return False
    operands = value.values
    if len(operands) < 3:
        return False
    if not _is_getattr_attr(operands[0], "agent", "model"):
        return False
    if _bool_or_operand_text(operands[1]) != "self.model":
        return False
    return isinstance(operands[-1], (ast.Constant, ast.Call))


def find_model_resolution_assign(
    snapshot_func: ast.AST,
) -> Optional[Tuple[ast.Assign, int]]:
    """Locate the single `model_name = <fallback chain>` assignment.

    Returns the assignment node and the 1-based source line it occupies. The
    search is scoped to the snapshot function so an unrelated `model_name`
    elsewhere in the module can never be mistaken for the anchor.
    """
    for node in ast.walk(snapshot_func):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not (isinstance(target, ast.Name) and target.id == "model_name"):
            continue
        if not is_model_resolution_expression(node.value):
            continue
        return node, int(getattr(node, "lineno", 0))
    return None


def model_resolution_anchor_found(snapshot_func: Optional[ast.AST]) -> bool:
    if snapshot_func is None:
        return False
    return find_model_resolution_assign(snapshot_func) is not None


def snapshot_priority(snapshot_func: ast.AST) -> Tuple[bool, str]:
    source = ast.unparse(snapshot_func) if hasattr(ast, "unparse") else ast.dump(snapshot_func)
    checks = {
        "last_actual": "_last_actual_response_model" in source,
        "agent_model": "getattr(agent, 'model'" in source or 'getattr(agent, "model"' in source,
        "self_model": "self.model" in source,
        "unknown": "unknown" in source,
    }
    if all(checks.values()):
        return True, "SNAPSHOT_PRIORITY_VALID"
    missing = [name for name, present in checks.items() if not present]
    return False, "SNAPSHOT_MISSING_" + "_".join(missing).upper()


def inspect_contract(repo_path: str) -> Dict[str, Any]:
    intake_path = os.path.join(repo_path, "agent/turn_response_intake.py")
    mixin_path = os.path.join(repo_path, "hermes_cli/cli_status_bar_mixin.py")
    intake_src = read_file(intake_path)
    mixin_src = read_file(mixin_path)
    result: Dict[str, Any] = {
        "intake_path": intake_path,
        "mixin_path": mixin_path,
        "intake_src": intake_src,
        "mixin_src": mixin_src,
        "strategy": "NONE",
        "capture_function_found": False,
        "response_bound": False,
        "response_model_read": False,
        "nonempty_guard": False,
        "capture_anchor_found": False,
        "capture_assignment": False,
        "snapshot_function_found": False,
        "snapshot_anchor_found": False,
        "snapshot_last_actual": False,
        "snapshot_priority": False,
    }
    if intake_src is None or mixin_src is None:
        return result
    intake_tree, intake_error = parse_ast(intake_src, intake_path)
    mixin_tree, mixin_error = parse_ast(mixin_src, mixin_path)
    result["intake_tree"] = intake_tree
    result["mixin_tree"] = mixin_tree
    result["parse_error"] = intake_error or mixin_error
    if intake_tree is None or mixin_tree is None:
        return result
    func = find_normalize_model_response(intake_tree)
    snapshot = find_get_status_bar_snapshot(mixin_tree)
    result["capture_function_found"] = func is not None
    result["snapshot_function_found"] = snapshot is not None
    if func is not None:
        result["response_bound"] = response_is_in_scope(func)[0]
        result["response_model_read"] = has_response_model_read(func)
        result["nonempty_guard"] = has_nonempty_guard(func)
        result["capture_assignment"] = assignment_to_last_actual(func)
        result["capture_anchor_found"] = "agent, response, messages" in intake_src and "splice_provider_projection(agent, response, messages)" in intake_src
        result["strategy"] = detect_strategy(intake_tree)
    if snapshot is not None:
        result["snapshot_last_actual"] = has_snapshot_last_actual(snapshot)
        result["snapshot_priority"] = snapshot_priority(snapshot)[0]
        # Structural anchor: the chain shape is the contract, not the literal
        # terminal fallback, which upstream internationalized.
        result["snapshot_anchor_found"] = model_resolution_anchor_found(snapshot)
    result["all_ok"] = all(
        result[key]
        for key in (
            "capture_function_found", "response_bound", "response_model_read",
            "nonempty_guard", "capture_assignment", "snapshot_function_found",
            "snapshot_last_actual", "snapshot_priority",
        )
    )
    return result


def print_contract(result: Dict[str, Any]) -> None:
    labels = (
        ("CAPTURE_FUNCTION_FOUND", "capture_function_found"),
        ("CAPTURE_RESPONSE_BOUND", "response_bound"),
        ("CAPTURE_RESPONSE_MODEL_READ", "response_model_read"),
        ("CAPTURE_NONEMPTY_GUARD", "nonempty_guard"),
        ("CAPTURE_ASSIGNMENT_TO_LAST_ACTUAL", "capture_assignment"),
        ("SNAPSHOT_FUNCTION_FOUND", "snapshot_function_found"),
        ("SNAPSHOT_READS_LAST_ACTUAL", "snapshot_last_actual"),
        ("SNAPSHOT_PRIORITY_VALID", "snapshot_priority"),
    )
    for label, key in labels:
        print(f"{label}={'YES' if result.get(key) else 'NO'}")


def apply_reconstruction(repo_path: str) -> int:
    result = inspect_contract(repo_path)
    print(f"APPLY_STRATEGY={result['strategy']}")
    print(f"APPLY_CAPTURE_FUNCTION_FOUND={'YES' if result['capture_function_found'] else 'NO'}")
    print(f"APPLY_RESPONSE_BOUND={'YES' if result['response_bound'] else 'NO'}")
    print(f"APPLY_MODEL_SOURCE_FOUND={'YES' if result['response_model_read'] else 'NO'}")
    print(f"APPLY_CAPTURE_ANCHOR_FOUND={'YES' if result['capture_anchor_found'] else 'NO'}")
    print(f"APPLY_SNAPSHOT_FUNCTION_FOUND={'YES' if result['snapshot_function_found'] else 'NO'}")
    print(f"APPLY_SNAPSHOT_ANCHOR_FOUND={'YES' if result['snapshot_anchor_found'] else 'NO'}")
    if result.get("parse_error"):
        print(f"APPLY_FAILURE_STAGE=PARSE")
        print(f"APPLY_FAILURE_REASON={result['parse_error']}")
        return 2
    if not result["capture_function_found"] or not result["response_bound"]:
        print("APPLY_FAILURE_STAGE=CAPTURE_SCOPE")
        print("APPLY_FAILURE_REASON=normalize_model_response missing or response is not bound")
        return 2
    if not result["snapshot_function_found"]:
        print("APPLY_FAILURE_STAGE=SNAPSHOT_SCOPE")
        print("APPLY_FAILURE_REASON=_get_status_bar_snapshot missing")
        return 2

    intake_src = result["intake_src"]
    mixin_src = result["mixin_src"]
    changed = []

    # Architecture B/C: response.model exists in the hook, but the turn-level
    # persistence anchor is absent. Insert only the semantic capture block.
    capture_block = (
        "    # Capture the provider-resolved model for status-bar observability.\n"
        "    actual_response_model = str(getattr(response, \"model\", None) or \"\").strip()\n"
        "    if actual_response_model:\n"
        "        agent._last_actual_response_model = actual_response_model\n\n"
    )
    splice_anchor = "    splice_provider_projection(agent, response, messages)\n"
    if not result["capture_assignment"]:
        if splice_anchor not in intake_src:
            print("APPLY_FAILURE_STAGE=CAPTURE_ANCHOR")
            print("APPLY_FAILURE_REASON=splice_provider_projection anchor not found")
            return 1
        if not result["response_model_read"] and result["strategy"] not in ("B_TARGET",):
            print("APPLY_FAILURE_STAGE=MODEL_SOURCE")
            print("APPLY_FAILURE_REASON=response.model source not found")
            return 1
        intake_src = intake_src.replace(splice_anchor, splice_anchor + "\n" + capture_block, 1)
        changed.append(result["intake_path"])

    # Mixin target anchor: preserve all existing rendering logic and only make
    # the real-turn value the first fallback in the existing model expression.
    # The upstream terminal fallback is carried through verbatim: it is not part
    # of the customization contract and changed once already ("unknown" ->
    # t("cli.shared.unknown")). Re-emitting a hardcoded literal here is what
    # broke reconstruction on newer targets.
    if not result["snapshot_last_actual"]:
        snapshot_func = find_get_status_bar_snapshot(result["mixin_tree"])
        if snapshot_func is None:
            print("APPLY_FAILURE_STAGE=SNAPSHOT_SCOPE")
            print("APPLY_FAILURE_REASON=_get_status_bar_snapshot missing")
            return 2
        anchor = find_model_resolution_assign(snapshot_func)
        if anchor is None:
            print("APPLY_FAILURE_STAGE=MIXIN_ANCHOR")
            print("APPLY_FAILURE_REASON=existing model resolution anchor not found")
            return 1
        assign_node, lineno = anchor
        source_lines = mixin_src.splitlines(keepends=True)
        if lineno < 1 or lineno > len(source_lines):
            print("APPLY_FAILURE_STAGE=MIXIN_ANCHOR")
            print("APPLY_FAILURE_REASON=anchor line out of range")
            return 1
        original_line = source_lines[lineno - 1]
        match = re.match(
            r"^(?P<indent>[ \t]*)model_name = \((?P<chain>.+)\)[ \t]*$",
            original_line.rstrip("\n"),
        )
        if match is None:
            print("APPLY_FAILURE_STAGE=MIXIN_ANCHOR")
            print("APPLY_FAILURE_REASON=anchor line shape not recognized")
            return 1
        indent = match.group("indent")
        chain = match.group("chain")
        new_model_block = (
            f"{indent}# Prefer the model actually returned for the current turn.\n"
            f'{indent}last_real = getattr(agent, "_last_actual_response_model", None) or None\n'
            f"{indent}model_name = (last_real or {chain})\n"
        )
        source_lines[lineno - 1] = new_model_block
        mixin_src = "".join(source_lines)
        changed.append(result["mixin_path"])

    if not changed:
        print("APPLY=NO_CHANGE")
        print("APPLY_CHANGED_FILES=NO")
        return 0

    # Parse before writing; this keeps a malformed reconstruction out of TEMP.
    if parse_ast(intake_src, result["intake_path"])[0] is None or parse_ast(mixin_src, result["mixin_path"])[0] is None:
        print("APPLY_FAILURE_STAGE=POST_WRITE_PARSE")
        print("APPLY_FAILURE_REASON=generated source failed AST parse")
        return 2
    write_file(result["intake_path"], intake_src)
    write_file(result["mixin_path"], mixin_src)
    print(f"APPLY_CHANGED_FILES={len(changed)}")
    for path in changed:
        print(f"APPLY_CHANGED={path}")
    print("APPLY=PASS")
    print("APPLY_FAILURE_STAGE=NONE")
    print("APPLY_FAILURE_REASON=NONE")
    return 0


def check(repo_path: str) -> int:
    result = inspect_contract(repo_path)
    if result.get("intake_src") is None or result.get("mixin_src") is None:
        print("UPSTREAM_ARCHITECTURE_CHANGED: required file missing")
        return 2
    if result.get("parse_error"):
        print(f"UPSTREAM_ARCHITECTURE_CHANGED: {result['parse_error']}")
        return 2
    print_contract(result)
    if result["all_ok"]:
        print("PASS_EXISTING")
        return 0
    missing = [
        key for key in (
            "capture_function_found", "response_bound", "response_model_read",
            "nonempty_guard", "capture_assignment", "snapshot_function_found",
            "snapshot_last_actual", "snapshot_priority",
        ) if not result.get(key)
    ]
    print("NEEDS_REIMPLEMENTATION: " + ", ".join(missing))
    return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=REPO)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.check:
        return check(args.repo)
    return apply_reconstruction(args.repo)


if __name__ == "__main__":
    raise SystemExit(main())
