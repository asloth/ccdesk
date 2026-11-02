"""Key names to Linux input (evdev) keycodes.

ydotool writes to /dev/uinput, so it takes raw keycodes, not key names, and it is the only
keyboard available where the compositor offers no virtual-keyboard protocol (KDE Plasma, and
any Wayland compositor without wtype). The names here are the ones `parsing.normalize_key`
produces, so a key that xdotool or wtype accepts can also go through ydotool.
"""

from __future__ import annotations

from ccdesk.errors import UnsupportedError
from ccdesk.parsing import MODIFIER_ORDER, KeyCombo

# The left-hand modifier of each family: pressing either works, and apps see the same modifier.
MODIFIER_CODES = {"ctrl": 29, "alt": 56, "shift": 42, "super": 125}

KEY_CODES = {
    "Escape": 1, "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8, "8": 9, "9": 10, "0": 11,
    "minus": 12, "equal": 13, "BackSpace": 14, "Tab": 15,
    "q": 16, "w": 17, "e": 18, "r": 19, "t": 20, "y": 21, "u": 22, "i": 23, "o": 24, "p": 25,
    "bracketleft": 26, "bracketright": 27, "Return": 28,
    "a": 30, "s": 31, "d": 32, "f": 33, "g": 34, "h": 35, "j": 36, "k": 37, "l": 38,
    "semicolon": 39, "apostrophe": 40, "grave": 41, "backslash": 43,
    "z": 44, "x": 45, "c": 46, "v": 47, "b": 48, "n": 49, "m": 50,
    "comma": 51, "period": 52, "slash": 53, "space": 57, "Caps_Lock": 58,
    "F1": 59, "F2": 60, "F3": 61, "F4": 62, "F5": 63, "F6": 64, "F7": 65, "F8": 66,
    "F9": 67, "F10": 68, "F11": 87, "F12": 88,
    "Print": 99, "Home": 102, "Up": 103, "Page_Up": 104, "Left": 105, "Right": 106, "End": 107,
    "Down": 108, "Page_Down": 109, "Insert": 110, "Delete": 111, "Menu": 139,
}
# F13 to F24 sit apart from F1 to F12 in the kernel's table.
KEY_CODES.update({f"F{n}": 183 + n - 13 for n in range(13, 25)})

# Characters that are a US-layout key with shift held. normalize_key() keeps a one-character
# key as typed, so "?" has to become "slash" plus shift to arrive as "?".
SHIFTED = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8",
    "(": "9", ")": "0", "_": "minus", "+": "equal", "plus": "equal",
    "{": "bracketleft", "}": "bracketright", "|": "backslash", ":": "semicolon",
    '"': "apostrophe", "<": "comma", ">": "period", "?": "slash", "~": "grave",
}


def keycode(key: str) -> tuple[int, bool]:
    """(evdev code, needs shift) for a normalized key name."""
    if key in MODIFIER_CODES:  # a bare modifier, like parse_key_combo("super")
        return MODIFIER_CODES[key], False
    if key in KEY_CODES:
        return KEY_CODES[key], False
    if key in SHIFTED:
        return KEY_CODES[SHIFTED[key]], True
    if len(key) == 1 and key.isupper():  # "A" is "a" with shift
        return KEY_CODES[key.lower()], True
    raise UnsupportedError(f"ydotool has no keycode for {key!r}; use type_text for text")


def ydotool_key_sequence(combo: KeyCombo) -> list[str]:
    """The `code:state` arguments of `ydotool key`: modifiers down, key down and up, modifiers up."""
    code, shift = keycode(combo.key)
    held = set(combo.modifiers) | ({"shift"} if shift else set())
    mods = [m for m in MODIFIER_ORDER if m in held]  # a stable order, so tests can read it
    return [
        *[f"{MODIFIER_CODES[m]}:1" for m in mods],
        f"{code}:1", f"{code}:0",
        *[f"{MODIFIER_CODES[m]}:0" for m in reversed(mods)],
    ]
