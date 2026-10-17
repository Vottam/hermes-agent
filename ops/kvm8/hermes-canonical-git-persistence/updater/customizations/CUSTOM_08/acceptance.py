#!/usr/bin/env python3
"""CUSTOM_08 acceptance — o lifecycle PM dos wrappers está normalizado e é DURÁVEL.

Este gate responde a pergunta que a FASE 15 da tarefa faz: um update futuro pode
restaurar wrappers legados? A resposta exige dois predicados verificáveis:

  1. ESTADO: todo wrapper operacional de perfil executa o PM launcher canônico,
     com HERMES_HOME próprio, -p literal, e Python do PM (não 3.11 legado).
  2. DURABILIDADE: o source-of-truth (/opt/mastertrack-hermes) possui um
     reconciler registrado que reconstrói esse estado, para que um update não
     reverta silenciosamente a correção.

Aceita: --repo <install_root>. Sem LLM, sem rede, sem escrita em /root/.local/bin.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import re
import sys
from pathlib import Path

SOT = Path(os.environ.get("HERMES_CUSTOM08_SOT", "/opt/mastertrend-hermes"))
CUSTOM = SOT / "customizations" / "CUSTOM_08"


def _load_reconcile():
    spec = importlib.util.spec_from_file_location("c08acc", CUSTOM / "reconcile.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {CUSTOM / 'reconcile.py'}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def check_source_of_truth() -> list[str]:
    """O reconciler precisa estar REGISTRADO, não só existir no disco."""
    fails: list[str] = []
    manifest = SOT / "customizations.yaml"
    if not manifest.is_file():
        return [f"FAIL: customizations.yaml ausente em {SOT}"]

    text = manifest.read_text(encoding="utf-8")
    if "CUSTOM_08" not in text:
        fails.append("FAIL: CUSTOM_08 não registrado em customizations.yaml")
        return fails

    block = text.split("---", 1)
    for required in ("customizations/CUSTOM_08/reconcile.py",
                     "customizations/CUSTOM_08/acceptance.py"):
        if required not in text:
            fails.append(f"FAIL: {required} não referenciado no manifest")
    return fails


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    args = ap.parse_args()

    c08 = _load_reconcile()
    install_root = Path(args.repo).resolve()

    print("ACCEPTANCE: CUSTOM_08 — PM lifecycle dos wrappers de perfil")
    print(f"ACCEPTANCE_PM_LAUNCHER={c08._pm_launcher(install_root)}")

    states = [c08.inspect(p, install_root) for p in c08.discover_wrappers()]
    targets = c08.profile_wrappers(states)
    print(f"ACCEPTANCE_PROFILE_WRAPPERS={len(targets)}")

    fails: list[str] = []
    if not targets:
        fails.append("FAIL: nenhum wrapper de perfil encontrado (gate não pode passar no vazio)")

    # Predicado 1 — estado desejado nos wrappers reais.
    problems = c08.evaluate(states, install_root)
    for p in problems:
        print(f"  ! {p}")
    fails.extend(problems)

    # Predicado 1b — o runtime efetivo é o Python do PM, não o 3.11 legado.
    ok_runtime, detail = c08.runtime_python_version(install_root)
    print(f"ACCEPTANCE_RUNTIME_PYTHON={'OK' if ok_runtime else 'FAIL'} ({detail})")
    if not ok_runtime:
        fails.append(f"FAIL: runtime Python inválido ({detail})")

    # Predicado 1c — o doctor upstream não pode ler nenhum alias órfão. O doctor
    # resolve o alvo pela regex `hermes -p (\S+)`; replicamos a mesma leitura.
    for s in targets:
        if s.p_literal and not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", s.p_literal):
            fails.append(
                f"FAIL: doctor leria alias órfão em {s.name} "
                f"(hermes -p {s.p_literal!r} não resolve para um profile)"
            )

    # Predicado 2 — durabilidade: o source-of-truth reconstrói esse estado.
    sot_fails = check_source_of_truth()
    for f in sot_fails:
        print(f"  ! {f}")
    fails.extend(sot_fails)
    if not sot_fails:
        print("ACCEPTANCE_SOURCE_OF_TRUTH=REGISTERED (reconciler em customizations.yaml)")

    for name in ("reconcile.py", "acceptance.py", "test_reconcile.py"):
        if not (CUSTOM / name).is_file():
            fails.append(f"FAIL: {CUSTOM / name} ausente")

    if fails:
        print(f"CUSTOM_08_ACCEPTANCE=FAIL ({len(fails)} falha(s))")
        return 1
    print("CUSTOM_08_ACCEPTANCE=PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
