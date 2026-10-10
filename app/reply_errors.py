"""Explicit recovery signals; an unknown dispatch is never retryable."""
from app.viber import ViberError


class ReplyRetryable(ValueError):
    def __init__(self, message, code='temporary_failure'):
        super().__init__(message)
        self.code = code


class ViberRetryable(ViberError):
    def __init__(self, message, code='viber_not_ready'):
        super().__init__(message)
        self.code = code


class BridgeUnavailable(ViberError):
    """Transport failed; the caller must establish whether input was attempted."""
