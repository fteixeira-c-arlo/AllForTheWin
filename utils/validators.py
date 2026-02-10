"""Input validation utilities."""
import re
from typing import Tuple


def validate_ipv4(value: str) -> Tuple[bool, str]:
    """
    Validate IPv4 address. Returns (is_valid, error_message).
    """
    value = value.strip()
    if not value:
        return False, "IP address cannot be empty."
    # Simple IPv4 regex: four octets 0-255
    pattern = re.compile(
        r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$"
    )
    m = pattern.match(value)
    if not m:
        return False, "Invalid IPv4 format. Use dotted decimal (e.g. 192.168.1.100)."
    for g in m.groups():
        if int(g) > 255:
            return False, "Each octet must be 0-255."
    return True, ""


def validate_port(value: str, default: int | None = None) -> Tuple[bool, str, int | None]:
    """
    Validate port number 1-65535. Returns (is_valid, error_message, port_int).
    If value is empty and default is set, returns (True, "", default).
    """
    value = value.strip()
    if default is not None and value == "":
        return True, "", default
    if not value:
        return False, "Port cannot be empty.", None
    try:
        port = int(value)
    except ValueError:
        return False, "Port must be a number.", None
    if port < 1 or port > 65535:
        return False, "Port must be between 1 and 65535.", None
    return True, "", port


def validate_model_name(value: str, valid_names: list[str]) -> Tuple[bool, str]:
    """
    Validate that value is one of the valid model names (case-insensitive).
    Returns (is_valid, error_message).
    """
    value = value.strip().upper()
    if not value:
        return False, "Model name cannot be empty."
    if value in [n.upper() for n in valid_names]:
        return True, ""
    return False, f"Unknown model '{value}'. Use list-models to see available models."


# Firmware version pattern: X.X.XX_XXXXXXX (e.g. 5.0.18_9a7a4d7)
FIRMWARE_VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+_[a-fA-F0-9]{7}$")


def validate_firmware_version(value: str) -> Tuple[bool, str]:
    """
    Validate firmware version format X.X.XX_XXXXXXX.
    Returns (is_valid, error_message).
    """
    value = value.strip()
    if not value:
        return False, "Firmware version cannot be empty."
    if FIRMWARE_VERSION_PATTERN.match(value):
        return True, ""
    return False, "Use format X.X.XX_XXXXXXX (e.g. 5.0.18_9a7a4d7)."
