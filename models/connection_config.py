"""Connection configuration data structure."""
from dataclasses import dataclass
from typing import Any, Optional
from datetime import datetime


@dataclass
class ConnectionConfig:
    """Active connection configuration."""

    type: str  # "ADB", "SSH", or "UART"
    settings: dict[str, Any]
    status: str = "disconnected"  # "connected" or "disconnected"
    connected_at: Optional[str] = None
    device_identifier: Optional[str] = None  # e.g. "192.168.1.100:5555"

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "settings": self.settings,
            "status": self.status,
            "connected_at": self.connected_at,
            "device_identifier": self.device_identifier,
        }
