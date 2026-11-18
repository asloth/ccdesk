"""Exception types shared by every layer of ccdesk."""


class CcdeskError(Exception):
    """Base class for all errors reported back to the caller.

    Optional structured fields, for the caller to act on without parsing prose: `code` (e.g.
    "gui_frame_stale"), `input_emitted` (input reached the backend before the error)
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
        """`code=gui_frame_stale input_emitted=false retry_same_request=false`, or None without a
        code. The MCP server puts it in front of the message."""
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


class CuaDriverError(CcdeskError):
    """A background refusal or uncertain delivery. Never forward foreground advice."""

    NEXT = {
        "target_occluded": "Target is occluded. Refresh the window list and screenshot, then wait for it to be uncovered or use a private screen.",
        "background_pointer_failed": "Background pointer delivery failed. Inspect the target before retrying; use a private screen if the route remains unavailable.",
        "popup_keyboard_grab": "A popup holds the keyboard. Inspect the target and dismiss its popup through accessibility, or use a private screen.",
        "cua_driver_disconnected": "The cua-driver connection was lost. Inspect the app before retrying, then restart this ccdesk server.",
    }
    # Said only where the driver documents that nothing was sent (0.38: it refuses "before
    # anything is sent" when a toolkit takes no routed input of that kind: Tk reads the hardware
    # pointer, Chromium and Electron drop routed pointer events).
    BEFORE_SENDING = frozenset({"background_unavailable"})
    # Words of the driver's own reasons that would steer an agent onto the user's focus.
    FOREGROUND = ("foreground", "bring_to_front", "front", "focus", "activate")

    def __init__(self, code: str, *, input_emitted: bool, tool: str | None = None, reason: str | None = None,
                 message: str | None = None):
        if message is None and code == "background_unavailable":
            what = f"this {tool}" if tool else "this input"
            why = f" (cua-driver: {reason})" if reason else ""
            if input_emitted:
                message = (f"cua-driver could not confirm {what} in the background{why}; it may have reached "
                           f"the app. Check with ui_tree() or a screenshot before repeating it.")
            else:
                message = (f"cua-driver refused {what} in the background before sending anything{why}: this "
                           f"window's toolkit does not take that kind of background input. Work through its "
                           f"accessibility tree instead (ui_tree(), then click_element() or set_value() on its "
                           f"refs); a task that needs this input needs a private screen.")
        if message is None:
            message = self.NEXT.get(code, "cua-driver could not complete background input. Inspect the app and refresh its state before choosing another action.")
        super().__init__(message, code=code, input_emitted=input_emitted)

    @classmethod
    def from_result(cls, result: dict, *, input_call: bool, tool: str | None = None):
        # v0.38 also uses action effects and delivery counts instead of a boolean.
        # Unknown/partial effects are conservatively emitted, to prevent duplicate input, except
        # for a refusal the driver documents as made before sending anything.
        code = result.get("code") or "cua_driver_error"
        emitted = result.get("input_emitted")
        if not isinstance(emitted, bool):
            effect = result.get("effect")
            delivery = result.get("delivery") or {}
            if effect is None and not delivery.get("delivered_count") and code in cls.BEFORE_SENDING:
                emitted = False
            else:
                emitted = bool(delivery.get("delivered_count")) or (input_call and effect not in ("refused", "none"))
        return cls(code, input_emitted=emitted, tool=tool, reason=cls._reason(result.get("reason")))

    @classmethod
    def _reason(cls, reason) -> str | None:
        """The driver's reason, short, and only when it carries no foreground advice."""
        if not isinstance(reason, str) or not reason.strip():
            return None
        text = " ".join(reason.split())[:160]
        return None if any(word in text.lower() for word in cls.FOREGROUND) else text


class StaleFrameError(PolicyError):
    """Coordinates came from a screenshot whose window layout is no longer the one on screen."""

    code = "gui_frame_stale"


class TargetRequiredError(PolicyError):
    """An agent has not chosen its window yet: its input would go to the user's."""

    code = "gui_target_required"


class TargetHiddenError(PolicyError):
    """The agent's window is minimized or on another desktop. Showing it takes the user's focus."""

    code = "gui_target_hidden"


class TargetCoveredError(PolicyError):
    """Another window (the user's or another agent's) is over the spot the agent meant to click."""

    code = "gui_target_covered"
    retry_same_request = True  # it may be gone after a wait()
