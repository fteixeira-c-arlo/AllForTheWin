"""Connection configuration data structure."""
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class ConnectionConfig:
    """Active connection configuration."""

    type: str  # "ADB", "SSH", or "UART"
    settings: dict[str, Any]
    status: str = "disconnected"  # "connected" or "disconnected"
    connected_at: Optional[str] = None
    device_identifier: Optional[str] = None  # e.g. "192.168.1.100:5555"
