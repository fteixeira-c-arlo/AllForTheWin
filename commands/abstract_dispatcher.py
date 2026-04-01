"""Resolve and execute abstract commands from JSON definitions (device-agnostic)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any


def load_abstract_definitions(path: str) -> list[dict]:
    """Load and return the list from a JSON file of abstract command definitions."""
    p = Path(path)
    with p.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"Abstract definitions file must contain a JSON array: {path}")
    return data


def find_abstract(name: str, definitions: list[dict]) -> dict | None:
    """Find an abstract command definition by name (case-insensitive)."""
    key = (name or "").strip().lower()
    if not key:
        return None
    for d in definitions:
        if not isinstance(d, dict):
            continue
        n = (d.get("name") or "").strip().lower()
        if n == key:
            return d
    return None


def resolve_step(raw_name: str, step_args: list[str], device_commands: list[dict]) -> str:
    """
    Look up the raw command by name in device_commands; return shell + args as one string.
    Raises ValueError if the raw command is not in the catalog.
    """
    want = (raw_name or "").strip().lower()
    shell = ""
    for c in device_commands:
        if not isinstance(c, dict):
            continue
        if (c.get("name") or "").strip().lower() == want:
            shell = c.get("shell")
            if shell is None:
                shell = ""
            else:
                shell = str(shell)
            break
    else:
        raise ValueError(
            f"Raw command '{raw_name}' is not defined in the device command catalog."
        )
    if step_args:
        return shell + " " + " ".join(step_args)
    return shell


def _norm_connection(connection_type: str) -> str:
    return (connection_type or "").strip().upper()


def _check_transport_restriction(
    restriction: str | None, connection_type: str
) -> None:
    """Raise ValueError if connection_type violates transport_restriction."""
    if restriction is None or restriction == "":
        return
    r = str(restriction).strip().lower()
    ct = _norm_connection(connection_type)
    if r == "no_uart":
        if ct == "UART":
            raise ValueError(
                f"This abstract command is not allowed over UART (transport_restriction=no_uart); "
                f"current connection is {connection_type!r}."
            )
    elif r == "adb_only":
        if ct != "ADB":
            raise ValueError(
                f"This abstract command requires ADB (transport_restriction=adb_only); "
                f"current connection is {connection_type!r}."
            )
    else:
        raise ValueError(
            f"Unknown transport_restriction value {restriction!r} in abstract definition."
        )


def _count_formal_args(arg_specs: list[Any]) -> tuple[int, int]:
    """Returns (required_count, optional_count) from abstract 'args' entries (optional ends with '?')."""
    required = 0
    optional = 0
    for spec in arg_specs or []:
        s = str(spec).strip()
        if s.endswith("?"):
            optional += 1
        else:
            required += 1
    return required, optional


def _validate_user_args(arg_specs: list[Any], user_args: list[str]) -> None:
    req, opt = _count_formal_args(arg_specs)
    n = len(user_args)
    if n < req:
        raise ValueError(
            f"Not enough arguments: expected at least {req}, got {n}."
        )
    if n > req + opt:
        raise ValueError(
            f"Too many arguments: expected at most {req + opt}, got {n}."
        )


def _args_per_step(
    sequence: list[Any], arg_specs: list[Any], user_args: list[str]
) -> list[list[str]]:
    """Split user_args across sequence steps (device-agnostic heuristics for current JSON shapes)."""
    seq = [str(s).strip() for s in (sequence or []) if str(s).strip()]
    if not seq:
        return []
    if len(seq) == 1:
        return [list(user_args)]
    req, _ = _count_formal_args(arg_specs)
    all_optional = bool(arg_specs) and req == 0
    if all_optional:
        out: list[list[str]] = [[] for _ in seq]
        out[-1] = list(user_args)
        return out
    out = [[] for _ in seq]
    out[0] = list(user_args)
    return out


def _interpret_execute_result(result: Any) -> tuple[bool, str]:
    """Support (bool, str) tuples from connection handlers; otherwise treat as success."""
    if isinstance(result, tuple) and len(result) == 2:
        ok, text = result[0], result[1]
        return bool(ok), str(text) if text is not None else ""
    if result is False:
        return False, "Command failed."
    return True, "" if result is None else str(result)


def execute_abstract_command(
    abstract_name: str,
    user_args: list[str],
    definitions: list[dict],
    device_commands: list[dict],
    execute_fn: Callable[[str], Any],
    connection_type: str,
) -> list[str] | None:
    """
    Run the abstract command's sequence via execute_fn(full_shell_string).

    Returns None if the abstract is unknown (caller may fall through) or if the
    sequence is empty (caller handles, e.g. push arlod).

    Raises ValueError for transport violations or bad user arg counts.
    Raises RuntimeError on step failure with step index, command, and error context.
    """
    abstract = find_abstract(abstract_name, definitions)
    if abstract is None:
        return None

    sequence = abstract.get("sequence") or []
    if not isinstance(sequence, list):
        sequence = []
    if len(sequence) == 0:
        return None

    _check_transport_restriction(abstract.get("transport_restriction"), connection_type)

    arg_specs = abstract.get("args") or []
    if not isinstance(arg_specs, list):
        arg_specs = []
    _validate_user_args(arg_specs, user_args)

    step_arg_lists = _args_per_step(sequence, arg_specs, user_args)
    outputs: list[str] = []

    for i, raw_step in enumerate(sequence):
        raw_name = str(raw_step).strip()
        step_args = step_arg_lists[i] if i < len(step_arg_lists) else []
        shell_line = resolve_step(raw_name, step_args, device_commands)
        try:
            result = execute_fn(shell_line)
        except Exception as e:
            raise RuntimeError(
                f"Abstract command {abstract_name!r} failed at step {i + 1}/{len(sequence)} "
                f"(raw={raw_name!r}): command {shell_line!r} raised {type(e).__name__}: {e}"
            ) from e
        ok, text = _interpret_execute_result(result)
        if not ok:
            raise RuntimeError(
                f"Abstract command {abstract_name!r} failed at step {i + 1}/{len(sequence)} "
                f"(raw={raw_name!r}): command {shell_line!r} — {text or 'Command failed.'}"
            )
        outputs.append(text)

    return outputs
