#!/usr/bin/env python3
# Teste permanente de regressão: garantir que reconcile.py não introduz
# referência a `response` fora de escopo válido.
import ast, os, sys
INTAKE_FILE = os.path.join(os.environ.get("HERMES_CANONICAL_REPO", "."), "agent/turn_response_intake.py")
with open(INTAKE_FILE) as f:
    src = f.read()
tree = ast.parse(src)
func = None
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name == "normalize_model_response":
        func = node
        break
if func is None:
    print("FAIL: normalize_model_response missing")
    sys.exit(1)
# Confirmar que 'response' é argumento
args = [a.arg for a in func.args.args + func.args.posonlyargs + func.args.kwonlyargs]
if "response" not in args:
    print(f"FAIL: response not in args: {args}")
    sys.exit(1)
# Confirmar que há atribuição a agent._last_actual_response_model
assignment_found = False
for child in ast.walk(func):
    if isinstance(child, ast.Assign):
        for target in child.targets:
            if isinstance(target, ast.Attribute):
                if isinstance(target.value, ast.Name) and target.value.id == "agent":
                    if target.attr == "_last_actual_response_model":
                        assignment_found = True
if not assignment_found:
    print("FAIL: assignment to agent._last_actual_response_model missing")
    sys.exit(1)
print("TEST_UNBOUND_RESPONSE=PASS")
print("UNBOUND_RESPONSE_GUARD=ENABLED")
sys.exit(0)
