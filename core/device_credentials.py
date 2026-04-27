"""
Device SSH/UART/ADB credential hints keyed by (model_id, stage).

Values mirror internal lab documentation; rotate in source if passwords change.
"""
from __future__ import annotations

from typing import Any, TypedDict


class CredentialRecord(TypedDict, total=False):
    model_ids: list[str]
    stage: str  # dev_qa | prod | all
    transport: str  # uart_ssh | adb | uboot
    username: str
    password: str
    note: str


# Pro 5 Phoenix/Griffin + Lory Dev/QA; Kea ADB; U-Boot strings stored as documentation.
DEVICE_CREDENTIALS: list[CredentialRecord] = [
    {
        "model_ids": ["VMC4060", "VMC4060P", "VMC4061"],
        "stage": "dev_qa",
        "transport": "uart_ssh",
        "username": "root",
        "password": "Q52NXfJf1V3A9O5khaAEXB63",
        "note": "UART/SSH Dev/QA",
    },
    {
        "model_ids": ["VMC4060", "VMC4060P", "VMC4061"],
        "stage": "dev_qa",
        "transport": "uart_ssh",
        "username": "root",
        "password": "ngbase",
        "note": "UART/SSH Dev/QA — < FW 1.21.0.0_1409",
    },
    {
        "model_ids": ["VMC4060", "VMC4060P", "VMC4061"],
        "stage": "prod",
        "transport": "uart_ssh",
        "username": "root",
        "password": "u6zb2rPkU6Dzd2p5kqoJwA2b",
        "note": "UART/SSH Prod 1.24+",
    },
    {
        "model_ids": ["VMC4060", "VMC4060P", "VMC4061"],
        "stage": "prod",
        "transport": "uart_ssh",
        "username": "root",
        "password": "nw2LuJ7syHKN9YUUHTfW7",
        "note": "UART/SSH Prod 1.23-",
    },
    {
        "model_ids": ["VMC4060", "VMC4060P", "VMC4061"],
        "stage": "all",
        "transport": "uboot",
        "username": "",
        "password": 'bjZ@H$`?DC8["fw%nZw5',
        "note": "U-Boot",
    },
    {
        "model_ids": ["VMC4070P"],
        "stage": "all",
        "transport": "adb",
        "username": "",
        "password": "arlo",
        "note": "ADB shell auth password",
    },
    {
        "model_ids": [
            "VMC3070",
            "VMC2070",
            "VMC3081",
            "VMC2081",
            "VMC3073",
            "VMC2073",
            "VMC3083",
            "VMC2083",
        ],
        "stage": "dev_qa",
        "transport": "adb",
        "username": "",
        "password": "arlo",
        "note": "ADB shell auth — E3 Wired Dev/QA",
    },
    {
        "model_ids": ["VMC3070", "VMC2070"],
        "stage": "prod",
        "transport": "adb",
        "username": "",
        "password": "fEn,Be}~L>%h;+Z?:8)N76G4g*y2JcAk",
        "note": "ADB shell auth — Dolphin Production",
    },
    {
        "model_ids": ["VMC3081", "VMC2081"],
        "stage": "prod",
        "transport": "adb",
        "username": "",
        "password": "j2W.YcSve~-_yKV=J+(@)DXma;R*?TG]",
        "note": "ADB shell auth — Orca Production",
    },
    {
        "model_ids": ["VMC3073", "VMC2073"],
        "stage": "prod",
        "transport": "adb",
        "username": "",
        "password": "j6v/XRuFeD2?8<~9BUQ:nP#JMmZSLb;f",
        "note": "ADB shell auth — Octopus Production",
    },
    {
        "model_ids": ["VMC3083", "VMC2083"],
        "stage": "prod",
        "transport": "adb",
        "username": "",
        "password": "cav8Re%T?dWf^Jz~9j;X}<&:2B,tkFxn",
        "note": "ADB shell auth — Jellyfish Production",
    },
    {
        "model_ids": ["AVD5001", "AVD6001"],
        "stage": "dev_qa",
        "transport": "uart_ssh",
        "username": "root",
        "password": "arlo",
        "note": "Lory Dev/QA UART/SSH",
    },
    {
        "model_ids": ["AVD5001", "AVD6001"],
        "stage": "prod",
        "transport": "uart_ssh",
        "username": "root",
        "password": "",
        "note": "Prod: use secured SSH/UART credentials page",
    },
    {
        "model_ids": ["VMB4540"],
        "stage": "dev_qa",
        "transport": "uart_ssh",
        "username": "root",
        "password": "ngbase",
        "note": "Osprey SmartHub Dev/QA UART/SSH",
    },
    {
        "model_ids": ["VMB4540"],
        "stage": "prod",
        "transport": "uart_ssh",
        "username": "root",
        "password": "F8krm9LYxwKAsUnVQFm98",
        "note": "Osprey SmartHub Prod/Staging UART/SSH (LCBS Gen3 latest)",
    },
    {
        "model_ids": ["VMB4540"],
        "stage": "prod",
        "transport": "uart_ssh",
        "username": "root",
        "password": "NX9PvLX2L3YvhjBjVLi68yBA8",
        "note": "Osprey SmartHub Prod/Staging UART/SSH (LCBS Gen3 previous)",
    },
]


def _model_match(model_id: str, mids: list[str]) -> bool:
    u = (model_id or "").strip().upper()
    return u in {(m or "").strip().upper() for m in mids}


def get_credentials_for_model(
    model_id: str | None, stage: str | None = None, transport: str | None = None
) -> list[CredentialRecord]:
    """Return matching credential rows (may be empty). stage: dev_qa | prod | all."""
    if not model_id:
        return []
    want_stage = (stage or "").strip().lower() or None
    want_transport = (transport or "").strip().lower() or None
    out: list[CredentialRecord] = []
    for rec in DEVICE_CREDENTIALS:
        if not _model_match(model_id, rec.get("model_ids") or []):
            continue
        rs = (rec.get("stage") or "").strip().lower()
        if want_stage and rs not in ("all", want_stage):
            continue
        rt = (rec.get("transport") or "").strip().lower()
        if want_transport and rt != want_transport:
            continue
        out.append(rec)
    return out


def _pick_adb_password_from_rows(rows: list[CredentialRecord]) -> str | None:
    for rec in rows:
        p = rec.get("password")
        if p is not None and str(p).strip() != "":
            return str(p)
    return None


def get_adb_password_for_model(model_id: str | None, *, stage: str | None = None) -> str | None:
    """
    ADB shell auth password for a model_id.

    stage None (Dev/QA UI prefill): prefer dev_qa, then all, then prod.
    stage \"prod\": prefer prod-matched rows, then \"all\" (e.g. Kea).
    """
    if not model_id:
        return None
    mid = (model_id or "").strip()
    want = (stage or "").strip().lower() or None
    if want == "prod":
        p = _pick_adb_password_from_rows(
            get_credentials_for_model(mid, stage="prod", transport="adb")
        )
        if p:
            return p
        return _pick_adb_password_from_rows(
            get_credentials_for_model(mid, stage="all", transport="adb")
        )
    for st in ("dev_qa", "all", "prod"):
        rows = get_credentials_for_model(mid, stage=st, transport="adb")
        p = _pick_adb_password_from_rows(rows)
        if p:
            return p
    return None


def resolve_production_adb_password(selected_model: dict[str, Any] | None) -> str | None:
    """Try primary name and fw_search_models for a Production ADB password."""
    if not selected_model:
        return None
    ids: list[str] = []
    n = selected_model.get("name")
    if n:
        ids.append(str(n).strip())
    for x in selected_model.get("fw_search_models") or []:
        s = str(x).strip()
        if s and s.upper() not in {i.upper() for i in ids}:
            ids.append(s)
    for mid in ids:
        if not mid:
            continue
        p = get_adb_password_for_model(mid, stage="prod")
        if p:
            return p
    return None


def get_ssh_password_for_model(model_id: str | None, *, stage: str | None = None) -> str | None:
    """SSH/UART password for a model_id and stage (dev_qa | prod)."""
    if not model_id:
        return None
    mid = (model_id or "").strip()
    want = (stage or "").strip().lower() or None
    if want == "prod":
        p = _pick_adb_password_from_rows(
            get_credentials_for_model(mid, stage="prod", transport="uart_ssh")
        )
        if p:
            return p
        return _pick_adb_password_from_rows(
            get_credentials_for_model(mid, stage="all", transport="uart_ssh")
        )
    for st in ("dev_qa", "all", "prod"):
        rows = get_credentials_for_model(mid, stage=st, transport="uart_ssh")
        p = _pick_adb_password_from_rows(rows)
        if p:
            return p
    return None


def resolve_production_ssh_password(selected_model: dict[str, Any] | None) -> str | None:
    """Try primary name and fw_search_models for a Production SSH/UART password."""
    if not selected_model:
        return None
    ids: list[str] = []
    n = selected_model.get("name")
    if n:
        ids.append(str(n).strip())
    for x in selected_model.get("fw_search_models") or []:
        s = str(x).strip()
        if s and s.upper() not in {i.upper() for i in ids}:
            ids.append(s)
    for mid in ids:
        if not mid:
            continue
        p = get_ssh_password_for_model(mid, stage="prod")
        if p:
            return p
    return None
