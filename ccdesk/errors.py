"""Exception types shared by every layer of ccdesk."""


class CcdeskError(Exception):
    """Base class for all errors reported back to the caller."""


class ValidationError(CcdeskError, ValueError):
    """Arguments were malformed or out of range."""


class PolicyError(CcdeskError, PermissionError):
    """The action is blocked by the safety policy (read-only, launch, confirm)."""


class UnsupportedError(CcdeskError):
    """The current session or backend cannot perform this action."""


class CommandError(CcdeskError):
    """An external helper (xdotool, scrot, ...) failed or is missing."""
