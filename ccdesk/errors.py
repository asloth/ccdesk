"""Exception types shared by every layer of ccdesk."""


class CcdeskError(Exception):
    """Base class for all errors reported back to the caller.

    Optional structured fields, for the caller to act on without parsing prose: `code` (e.g.
    "stale_ref", "gui_frame_stale"), `input_emitted` (input reached the backend before the error)
    and `retry_same_request` (the identical call may succeed later). Anything else goes in `extra`.
    """

    code: str | None = None
    retry_same_request: bool = False

    def __init__(self, *args, code: str | None = None, input_emitted: bool = False,
                 retry_same_request: bool | None = None, **extra):
        super().__init__(*args)
        if code is not None:
            self.code = code
        self.input_emitted = input_emitted
        if retry_same_request is not None:
            self.retry_same_request = retry_same_request
        self.extra = extra

    def prefix(self) -> str | None:
        """`code=stale_ref input_emitted=false retry_same_request=false`, or None without a code."""
        if not self.code:
            return None
        return (f"code={self.code} input_emitted={str(bool(self.input_emitted)).lower()} "
                f"retry_same_request={str(bool(self.retry_same_request)).lower()}")


class ValidationError(CcdeskError, ValueError):
    """Arguments were malformed or out of range."""


class PolicyError(CcdeskError, PermissionError):
    """The action is blocked by the safety policy (read-only, launch, confirm)."""


class UnsupportedError(CcdeskError):
    """The current session or backend cannot perform this action."""


class CommandError(CcdeskError):
    """An external helper (xdotool, scrot, ...) failed or is missing."""


class StaleFrameError(PolicyError):
    """Coordinates came from a screenshot whose window layout is no longer the one on screen."""

    code = "gui_frame_stale"
