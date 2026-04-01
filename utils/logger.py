"""Logging utilities."""
import logging
import sys

_logger: logging.Logger | None = None


def get_logger(name: str = "arlo_terminal", level: int = logging.INFO) -> logging.Logger:
    """Return a logger for the application. Console handler only by default."""
    global _logger
    if _logger is not None:
        return _logger
    _logger = logging.getLogger(name)
    _logger.setLevel(level)
    if not _logger.handlers:
        h = logging.StreamHandler(sys.stderr)
        h.setLevel(level)
        _logger.addHandler(h)
    return _logger
