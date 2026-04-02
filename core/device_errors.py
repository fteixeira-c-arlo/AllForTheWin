"""Typed errors for device connection and abstract command routing."""


class UnsupportedConnectionError(Exception):
    """Raised when the user selects a transport the device does not support (e.g. ADB on AmebaPro2)."""


class UnknownDeviceError(Exception):
    """Raised when a detected model ID is not present in the device registry."""


class CommandNotSupportedError(Exception):
    """Raised when an abstract or catalog command is not valid for the device's platform or variant."""


class MCUConsoleNotConnectedError(Exception):
    """Gen5: MCU CLI commands require a separate MCU UART session; ISP shell alone is insufficient."""
