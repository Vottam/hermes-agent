#!/usr/bin/env python3
"""Per-profile operational classifier for the KVM8 updater deep-runtime gates.

This module is the single discovery point shared by every deep gate. It is
read-only: it never installs, enables, mutates, or prompts. Its only job is to
turn the real filesystem state of every distinct operational profile into a
deterministic, fail-closed classification.

Fail-closed rule (G1): a profile that cannot be classified is FAIL, never a
PASS by omission.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Distinct operational root profiles. hermes-statusbar is a legitimate alias
# of hermes-kvm8 (same HERMES_HOME), so it is not a distinct profile.
ROOT_PROFILE_ROOT = Path("/root/.hermes/profiles")
DEV_PROFILE_ROOT = Path("/home/mpq-dev/.hermes/profiles")

DEV_USER = "mpq-dev"
DEV_HOME = "/home/mpq-dev"
DEV_WORKSPACE = "/srv/monepeviq/dev"
DEV_LAUNCHER = Path("/usr/local/bin/hermes-dev")
PM_LAUNCHER = os.environ.get(
    "HERMES_CANONICAL_PM_LAUNCHER",
    "/opt/hermes-agent-clean.release-0.22.2-hermes-kvm8-0222-20261016T005835Z/.hermes/bin/hermes",
)

REQUIRED_PYTHON = "3.14.7"

# The install root is the PM launcher's grandparent; the catalog pin is read
# from there, never hardcoded in this module.
INSTALL_ROOT = Path(PM_LAUNCHER).parent.parent.parent

HINDSIGHT_LOWER = (0, 10, 0)
HINDSIGHT_UPPER = (0, 11, 0)  # exclusive

# PM publication artifacts that are legitimately persistent and are therefore
# NOT residual staging. Derived from the PM source, not guessed:
#   - plugins_cmd_install.py:313  TemporaryDirectory(prefix=".install-") -> staging,
#     removed by the context manager on success, so a leftover .install-* DIR is a fault.
#   - plugins_cmd_git.py:33-53    .install-metadata.json + its atomic-write .lock
#     are persistent owner files of the installed state.
PERSISTENT_INSTALL_ARTIFACTS = frozenset({
    ".install-metadata.json",
    ".install-metadata.json.lock",
})


def catalog_pin_sha(repo: Path = INSTALL_ROOT) -> str:
    """Read the immutable 40-hex catalog pin for hindsight from plugin-catalog."""
    path = Path(repo) / "plugin-catalog" / "hindsight.yaml"
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = re.match(r"^sha:\s*[\"']?([0-9a-fA-F]{40})[\"']?\s*$", line.strip())
        if match:
            return str(match.group(1))
    return ""


#: Reason string used when the interpreter cannot import PyYAML. This is a
#: RUNTIME defect of the process running this gate, NOT a property of the
#: profile: every profile fails it identically, and no profile can be
#: classified at all. It is named distinctly from any config-level reason so
#: a caller can never confuse "could not read the config" with "there is no
#: config / the config has no memory section".
YAML_MODULE_UNAVAILABLE = "gate_runtime_dependency_missing:yaml"

#: Set by :func:`require_yaml_runtime` for the process-wide state. Kept here
#: (module scope) so every discovery call in the same process agrees.
_YAML_RUNTIME_ERROR: str = ""


def yaml_runtime_status() -> str:
    """Empty string when PyYAML is importable, else the fail-loud reason.

    Public on purpose: the reconcilers and the deep gates call this so a
    missing dependency is reported as a runtime defect with a name, instead
    of being laundered into an empty profile set.
    """
    try:
        import yaml  # type: ignore  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return f"{YAML_MODULE_UNAVAILABLE}:{type(exc).__name__}"
    return ""


def require_yaml_runtime() -> str:
    """Return "" if the profile-discovery runtime is capable, else the reason.

    KVM8 2026-09-27. Before this existed, running a gate under an interpreter
    without PyYAML silently produced zero discoverable profiles: every profile
    came back ``PROFILE_UNCLASSIFIABLE:yaml_module_unavailable``,
    ``hindsight_required`` was False for all of them, and the caller printed
    ``NO_HINDSIGHT_REQUIRED_PROFILES`` + ``BLOCKED_SAFE``. That message
    asserts a fact about the PROFILES when the real fact is about the
    INTERPRETER -- and an empty result can legitimately mean either, so the
    two must stay distinguishable. This function is that distinction.
    """
    global _YAML_RUNTIME_ERROR
    _YAML_RUNTIME_ERROR = yaml_runtime_status()
    return _YAML_RUNTIME_ERROR


def _load_config(home: Path) -> tuple[dict[str, Any] | None, str]:
    """Parse config.yaml (fail-closed on ambiguity).

    A missing PyYAML is reported as ``YAML_MODULE_UNAVAILABLE`` -- a named
    runtime dependency failure -- and never as a config-level reason. The
    caller distinguishes the two; see :func:`require_yaml_runtime`.
    """
    runtime_error = require_yaml_runtime()
    if runtime_error:
        return None, runtime_error

    for name in ("config.yaml", "config.yml"):
        path = home / name
        if not path.is_file():
            continue
        import yaml  # type: ignore  # proven importable by require_yaml_runtime()

        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace"))
        except Exception as exc:  # noqa: BLE001
            return None, f"config_parse_error:{type(exc).__name__}"
        if not isinstance(data, dict):
            return None, "config_not_a_mapping"
        return data, ""
    return None, "config_missing"


def _memory_section(config: dict[str, Any]) -> tuple[str, bool | None]:
    section = config.get("memory")
    if not isinstance(section, dict):
        return "", None
    provider = section.get("provider")
    provider = str(provider).strip().lower() if provider is not None else ""
    enabled = section.get("memory_enabled")
    if enabled is None:
        enabled = section.get("enabled")
    if enabled is None:
        return provider, None
    if isinstance(enabled, bool):
        return provider, enabled
    text = str(enabled).strip().lower()
    if text in {"true", "yes", "1", "on"}:
        return provider, True
    if text in {"false", "no", "0", "off"}:
        return provider, False
    return provider, None


def _enabled_plugins(home: Path) -> tuple[list[str] | None, str]:
    """Return the profile's enabled-plugin list, or (None, reason)."""
    plugins_dir = home / "plugins"
    for name in ("enabled.json", "plugins.json", "config.json"):
        path = plugins_dir / name
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except Exception as exc:  # noqa: BLE001
            return None, f"enabled_parse_error:{type(exc).__name__}:{name}"
        if isinstance(data, dict):
            for key in ("enabled", "plugins", "enabled_plugins"):
                if key in data:
                    value = data[key]
                    if isinstance(value, list):
                        return [str(item) for item in value], ""
                    if isinstance(value, dict):
                        return [str(item) for item in value], ""
        elif isinstance(data, list):
            return [str(item) for item in data], ""
    # Fall back to the profile config: plugins.enabled may live there.
    config, _ = _load_config(home)
    if isinstance(config, dict):
        section = config.get("plugins")
        if isinstance(section, dict):
            value = section.get("enabled")
            if isinstance(value, list):
                return [str(item) for item in value], ""
            if isinstance(value, dict):
                return [str(item) for item in value], ""
    return None, "enabled_list_not_found"


def _install_metadata(home: Path) -> tuple[dict[str, Any] | None, str]:
    path = home / "plugins" / ".install-metadata.json"
    if not path.is_file():
        return None, "install_metadata_missing"
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001
        return None, f"install_metadata_parse_error:{type(exc).__name__}"
    if not isinstance(data, dict):
        return None, "install_metadata_not_a_mapping"
    return data, ""


def _residual_staging(home: Path) -> list[str]:
    """Leftover .install-* staging entries are a hard failure (G1).

    Only genuine staging is reported. The PM's own persistent publication
    artifacts (metadata + its atomic-write lock) are excluded, otherwise every
    correctly installed profile would fail forever.
    """
    plugins_dir = home / "plugins"
    if not plugins_dir.is_dir():
        return []
    found: list[str] = []
    for child in sorted(plugins_dir.iterdir()):
        if not child.name.startswith(".install-"):
            continue
        if child.name in PERSISTENT_INSTALL_ARTIFACTS:
            continue
        found.append(child.name)
    return found


@dataclass
class Profile:
    name: str
    user: str
    home_root: str          # /root/.hermes  or  /home/mpq-dev/.hermes
    hermes_home: Path
    is_dev: bool
    wrapper: str | None
    launcher: str
    classification_ok: bool = False
    classification_reason: str = ""
    memory_provider: str = ""
    memory_enabled: bool | None = None
    hindsight_required: bool = False
    plugin_dir_present: bool = False
    plugin_enabled: bool = False
    install_metadata_ok: bool = False
    install_metadata_reason: str = ""
    pinned: bool = False
    revision: str = ""
    residual_staging: list[str] = field(default_factory=list)
    failure_reasons: list[str] = field(default_factory=list)

    @property
    def hermes_root(self) -> Path:
        """The PM store root that owns this profile.

        Derived from the real PM layout, not from the profile directory name:
        profiles live at <hermes_root>/profiles/<name>, so the store root is
        the profiles directory's parent. This is the same relationship the G1
        requirement expresses as <hermes_home>/../../installs.
        """
        return self.hermes_home.parent.parent

    @property
    def hindsight_install_roots(self) -> list[Path]:
        """PM dependency envs that can carry this profile's plugin deps."""
        root = self.hermes_root / "installs"
        if not root.is_dir():
            return []
        return sorted(root.glob("*/environments/*/venv"))

    def to_dict(self) -> dict[str, Any]:
        data = {
            "PROFILE": self.name,
            "USER": self.user,
            "HERMES_HOME": str(self.hermes_home),
            "HOME_ROOT": self.home_root,
            "HERMES_ROOT": str(self.hermes_root),
            "IS_DEV": "yes" if self.is_dev else "no",
            "WRAPPER": self.wrapper or "",
            "LAUNCHER": self.launcher,
            "CLASSIFICATION_OK": "yes" if self.classification_ok else "no",
            "CLASSIFICATION_REASON": self.classification_reason,
            "MEMORY_PROVIDER": self.memory_provider or "UNKNOWN",
            "MEMORY_ENABLED": "" if self.memory_enabled is None else str(self.memory_enabled).lower(),
            "HINDSIGHT_REQUIRED": "yes" if self.hindsight_required else "no",
            "PLUGIN_DIR_PRESENT": "yes" if self.plugin_dir_present else "no",
            "PLUGIN_ENABLED": "yes" if self.plugin_enabled else "no",
            "INSTALL_METADATA_OK": "yes" if self.install_metadata_ok else "no",
            "INSTALL_METADATA_REASON": self.install_metadata_reason,
            "PINNED": "yes" if self.pinned else "no",
            "REVISION": self.revision,
            "RESIDUAL_STAGING": ",".join(self.residual_staging),
            "FAILURE_REASONS": ",".join(self.failure_reasons),
        }
        return data


def _root_wrapper_for(name: str) -> str | None:
    candidate = Path("/root/.local/bin") / name
    return str(candidate) if candidate.is_file() else None


def classify_profile(home: Path, *, name: str, user: str, is_dev: bool,
                     launcher: str) -> Profile:
    profile = Profile(name=name, user=user, home_root=str(home.parent),
                      hermes_home=home, is_dev=is_dev,
                      wrapper=None if is_dev else _root_wrapper_for(name),
                      launcher=launcher)
    reasons: list[str] = []

    if not home.is_dir():
        profile.classification_reason = "hermes_home_missing"
        profile.failure_reasons.append("PROFILE_HERMES_HOME_MISSING")
        return profile

    config, reason = _load_config(home)
    if config is None:
        profile.classification_reason = reason
        profile.failure_reasons.append(f"PROFILE_UNCLASSIFIABLE:{reason}")
        return profile

    provider, enabled = _memory_section(config)
    profile.memory_provider = provider
    profile.memory_enabled = enabled

    if not provider:
        profile.classification_reason = "memory_provider_absent"
        profile.failure_reasons.append("PROFILE_UNCLASSIFIABLE:memory_provider_absent")
        return profile
    if enabled is None:
        profile.classification_reason = "memory_enabled_unparseable"
        profile.failure_reasons.append("PROFILE_UNCLASSIFIABLE:memory_enabled_unparseable")
        return profile

    profile.classification_ok = True
    profile.classification_reason = "ok"

    # G1: HINDSIGHT_REQUIRED only when the provider is active for that profile.
    profile.hindsight_required = (provider == "hindsight" and enabled is True)

    enabled_list, enabled_reason = _enabled_plugins(home)
    if enabled_list is not None:
        profile.plugin_enabled = "hindsight" in {item.lower() for item in enabled_list}
    else:
        reasons.append(f"ENABLED_LIST_UNREADABLE:{enabled_reason}")

    profile.residual_staging = _residual_staging(home)

    if profile.hindsight_required:
        plugin_dir = home / "plugins" / "hindsight"
        profile.plugin_dir_present = plugin_dir.is_dir() and any(plugin_dir.iterdir())
        if not profile.plugin_dir_present:
            reasons.append("HINDSIGHT_PLUGIN_DIR_MISSING")
        if enabled_list is None:
            reasons.append(f"PLUGIN_ENABLEMENT_UNVERIFIABLE:{enabled_reason}")
        elif not profile.plugin_enabled:
            reasons.append("HINDSIGHT_PLUGIN_NOT_ENABLED")

        metadata, meta_reason = _install_metadata(home)
        profile.install_metadata_reason = meta_reason
        if metadata is None:
            reasons.append(f"INSTALL_METADATA_INVALID:{meta_reason}")
        else:
            entry = metadata.get("hindsight")
            if not isinstance(entry, dict):
                reasons.append("INSTALL_METADATA_NO_HINDSIGHT_ENTRY")
            else:
                profile.pinned = entry.get("pinned") is True
                profile.revision = str(entry.get("revision", ""))
                expected_pin = catalog_pin_sha()
                if not profile.pinned:
                    reasons.append("INSTALL_METADATA_NOT_PINNED")
                if not expected_pin:
                    reasons.append("CATALOG_PIN_UNREADABLE")
                elif profile.revision != expected_pin:
                    reasons.append(
                        f"INSTALL_METADATA_PIN_MISMATCH:expected={expected_pin}:got={profile.revision}")
                profile.install_metadata_ok = not (
                    not profile.pinned
                    or not expected_pin
                    or profile.revision != expected_pin
                )
        if profile.residual_staging:
            reasons.append(f"RESIDUAL_INSTALL_STAGING:{','.join(profile.residual_staging)}")

    profile.failure_reasons.extend(reasons)
    return profile


def discover_profiles(include_dev: bool = True) -> list[Profile]:
    profiles: list[Profile] = []
    if ROOT_PROFILE_ROOT.is_dir():
        for home in sorted(p for p in ROOT_PROFILE_ROOT.iterdir() if p.is_dir()):
            if not (home / "config.yaml").is_file() and not (home / "config.yml").is_file():
                continue
            profiles.append(classify_profile(
                home, name=home.name, user="root", is_dev=False,
                launcher=_root_wrapper_for(home.name) or PM_LAUNCHER,
            ))
    if include_dev and DEV_PROFILE_ROOT.is_dir():
        for home in sorted(p for p in DEV_PROFILE_ROOT.iterdir() if p.is_dir()):
            if not (home / "config.yaml").is_file() and not (home / "config.yml").is_file():
                continue
            profiles.append(classify_profile(
                home, name=home.name, user=DEV_USER, is_dev=True,
                launcher=str(DEV_LAUNCHER),
            ))
    return profiles


#: Outcome of :func:`discover_profiles_checked`.
DISCOVERY_OK = "OK"
DISCOVERY_RUNTIME_UNABLE = "RUNTIME_UNABLE"
DISCOVERY_EMPTY = "EMPTY"


def discover_profiles_checked(include_dev: bool = True) -> tuple[list[Profile], str, str]:
    """Classify profiles and separate "none match" from "none could be read".

    KVM8 2026-09-27. This exists because an empty result is ambiguous on its
    own. Two completely different situations both yield zero hindsight-required
    profiles:

      EMPTY               -- profiles were read fine and none of them declare
                             ``memory.provider: hindsight``. Legitimate.
      RUNTIME_UNABLE      -- NOT ONE profile could be classified because the
                             interpreter lacks a required dependency. Never
                             legitimate; it is a broken gate pretending to be a
                             clean one.

    Returning them under the same shape is what let a missing PyYAML be
    reported as ``NO_HINDSIGHT_REQUIRED_PROFILES`` -- a claim about the
    profiles instead of about the interpreter. Callers that care must use this
    function and branch on the third element.

    Returns ``(profiles, state, detail)`` where ``state`` is one of the
    DISCOVERY_* constants and ``detail`` is empty unless something failed.
    """
    runtime_error = require_yaml_runtime()
    if runtime_error:
        # The interpreter cannot read ANY profile config. Refuse to present an
        # empty (and therefore authoritative-looking) profile list.
        return [], DISCOVERY_RUNTIME_UNABLE, runtime_error

    profiles = discover_profiles(include_dev=include_dev)
    if not profiles:
        return [], DISCOVERY_EMPTY, "no_profile_directory_present"

    unclassifiable = [p.name for p in profiles if not p.classification_ok]
    if len(unclassifiable) == len(profiles):
        # Every single profile failed. That is a systemic runtime/environment
        # condition, not a per-profile property, so it must not read as an
        # empty match set.
        return profiles, DISCOVERY_RUNTIME_UNABLE, (
            "all_profiles_unclassifiable:" + ",".join(sorted(unclassifiable))
        )
    return profiles, DISCOVERY_OK, (
        "" if not unclassifiable else "unclassifiable=" + ",".join(sorted(unclassifiable))
    )


def parse_version(text: str) -> tuple[int, ...] | None:
    match = re.match(r"^\s*(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)),
            int(match.group(3) or 0))


def version_in_policy(version: str) -> bool:
    """Effective Hindsight version must be >=0.10.0 and <0.11.0."""
    parsed = parse_version(version)
    if parsed is None:
        return False
    return HINDSIGHT_LOWER <= parsed < HINDSIGHT_UPPER


def read_effective_versions(venv: Path) -> dict[str, str]:
    """Read effective installed distribution versions from one PM venv.

    Uses importlib.metadata in the env's own interpreter. A missing
    distribution is reported as ABSENT (never omitted) so the caller can
    distinguish "env not read" from "env lacks the plugin deps".
    """
    python = venv / "bin" / "python"
    if not python.is_file():
        return {}
    import subprocess

    code = (
        "import importlib.metadata as m, json\n"
        "d = {}\n"
        "for name in ('hindsight-client', 'hindsight-embed'):\n"
        "    try:\n"
        "        d[name] = m.version(name)\n"
        "    except Exception:\n"
        "        d[name] = 'ABSENT'\n"
        "print(json.dumps(d))\n"
    )
    try:
        result = subprocess.run(
            [str(python), "-I", "-c", code],
            capture_output=True, text=True, timeout=120, check=False,
        )
    except Exception:  # noqa: BLE001
        return {}
    if result.returncode != 0 or not result.stdout.strip():
        return {}
    try:
        data = json.loads(result.stdout.strip().splitlines()[-1])
    except Exception:  # noqa: BLE001
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def emit(value: Any) -> None:
    if isinstance(value, Profile):
        value = value.to_dict()
    if isinstance(value, dict):
        for key, item in value.items():
            print(f"{key}={item}")
    else:
        print(value)
