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


def get_adb_password_for_model(model_id: str | None) -> str | None:
    """Default ADB auth password from registry/credentials for Kea."""
    for rec in DEVICE_CREDENTIALS:
        if (rec.get("transport") or "").lower() != "adb":
            continue
        if _model_match(model_id or "", rec.get("model_ids") or []):
            p = rec.get("password")
            return str(p) if p else None
    return None
