"""Pure parsing and validation helpers. No subprocesses, no I/O beyond reading a PNG header."""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass

from ccdesk.errors import ValidationError

MODIFIER_ALIASES = {
    "ctrl": "ctrl",
    "control": "ctrl",
    "ctl": "ctrl",
    "alt": "alt",
    "option": "alt",
    "opt": "alt",
    "shift": "shift",
    "super": "super",
    "meta": "super",
    "win": "super",
    "windows": "super",
    "cmd": "super",
    "command": "super",
    "logo": "super",
}
MODIFIER_ORDER = ("ctrl", "alt", "shift", "super")

# Friendly names -> X keysym names (also understood by wtype).
KEY_ALIASES = {
    "enter": "Return",
    "return": "Return",
    "esc": "Escape",
    "escape": "Escape",
    "tab": "Tab",
    "space": "space",
    "spacebar": "space",
    "backspace": "BackSpace",
    "bksp": "BackSpace",
    "delete": "Delete",
    "del": "Delete",
    "insert": "Insert",
    "ins": "Insert",
    "home": "Home",
    "end": "End",
    "pageup": "Page_Up",
    "page_up": "Page_Up",
    "pgup": "Page_Up",
    "pagedown": "Page_Down",
    "page_down": "Page_Down",
    "pgdn": "Page_Down",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "arrowup": "Up",
    "arrowdown": "Down",
    "arrowleft": "Left",
    "arrowright": "Right",
    "menu": "Menu",
    "capslock": "Caps_Lock",
    "printscreen": "Print",
    "print": "Print",
    "plus": "plus",
    "minus": "minus",
    "comma": "comma",
    "period": "period",
    "slash": "slash",
}
for _n in range(1, 25):
    KEY_ALIASES[f"f{_n}"] = f"F{_n}"

_KEYSYM_RE = re.compile(r"^[A-Za-z0-9_]+$")

# (modifiers, key) pairs that need confirm=true. Matching is "at least these modifiers",
# so Ctrl+Shift+W is treated as destructive too.
DESTRUCTIVE_COMBOS = (
    (frozenset({"alt"}), "f4"),
    (frozenset({"ctrl"}), "q"),
    (frozenset({"ctrl"}), "w"),
    (frozenset({"super"}), "q"),
)

BUTTONS = {"left": 1, "middle": 2, "right": 3}


@dataclass(frozen=True)
class KeyCombo:
    key: str
    modifiers: tuple[str, ...] = ()

    def xdotool(self) -> str:
        return "+".join([*self.modifiers, self.key])

    def __str__(self) -> str:
        return "+".join([*(m.capitalize() for m in self.modifiers), self.key])


def normalize_modifier(name: str) -> str:
    mod = MODIFIER_ALIASES.get(str(name).strip().lower())
    if mod is None:
        raise ValidationError(f"unknown modifier {name!r}; use one of {', '.join(MODIFIER_ORDER)}")
    return mod


def normalize_key(name: str) -> str:
    name = str(name).strip()
    if not name:
        raise ValidationError("key must not be empty")
    alias = KEY_ALIASES.get(name.lower().replace(" ", ""))
    if alias:
        return alias
    if len(name) == 1:
        return name
    if not _KEYSYM_RE.match(name):
        raise ValidationError(f"invalid key name {name!r}")
    return name


def parse_key_combo(key: str, modifiers: list[str] | tuple[str, ...] | None = None) -> KeyCombo:
    """Parse "ctrl+shift+t" or key="t", modifiers=["ctrl","shift"] into a KeyCombo."""
    if not isinstance(key, str) or not key.strip():
        raise ValidationError("key must be a non-empty string")
    if key.strip() == "+":
        parts = ["+"]
    elif key.endswith("++"):  # "ctrl++" means ctrl + plus
        parts = key[:-2].split("+") + ["+"]
    else:
        parts = key.split("+")
    if any(not p.strip() for p in parts):
        raise ValidationError(f"malformed key combo {key!r}")
    *inline_mods, base = parts
    mods = {normalize_modifier(m) for m in [*inline_mods, *(modifiers or [])]}
    if base.strip().lower() in MODIFIER_ALIASES and not inline_mods and not modifiers:
        # A bare modifier press like "super" is allowed as the key itself.
        return KeyCombo(key=normalize_modifier(base))
    return KeyCombo(
        key="plus" if base == "+" else normalize_key(base),
        modifiers=tuple(m for m in MODIFIER_ORDER if m in mods),
    )


def is_destructive(combo: KeyCombo) -> bool:
    mods = set(combo.modifiers)
    key = combo.key.lower()
    return any(key == k and need <= mods for need, k in DESTRUCTIVE_COMBOS)


def parse_button(button: str | int) -> int:
    if isinstance(button, int) and not isinstance(button, bool):
        if button in (1, 2, 3):
            return button
    elif isinstance(button, str) and button.strip().lower() in BUTTONS:
        return BUTTONS[button.strip().lower()]
    raise ValidationError(f"button must be one of {', '.join(BUTTONS)}, got {button!r}")


def check_int(name: str, value, minimum: int | None = None, maximum: int | None = None) -> int:
    if isinstance(value, bool):
        raise ValidationError(f"{name} must be an integer")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if not isinstance(value, int):
        raise ValidationError(f"{name} must be an integer, got {value!r}")
    if minimum is not None and value < minimum:
        raise ValidationError(f"{name} must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise ValidationError(f"{name} must be <= {maximum}, got {value}")
    return value


def check_point(x, y, screen: tuple[int, int] | None = None) -> tuple[int, int]:
    """Validate a screen coordinate; when the screen size is known, enforce bounds."""
    x = check_int("x", x, 0, screen[0] - 1 if screen else None)
    y = check_int("y", y, 0, screen[1] - 1 if screen else None)
    return x, y


@dataclass(frozen=True)
class Region:
    x: int
    y: int
    width: int
    height: int

    def geometry(self) -> str:
        return f"{self.width}x{self.height}+{self.x}+{self.y}"


_GEOM_RE = re.compile(r"^(\d+)x(\d+)\+(\d+)\+(\d+)$")


def parse_region(region, screen: tuple[int, int] | None = None) -> Region | None:
    """Accept "x,y,w,h", "WxH+X+Y", a 4-item list, or a dict with x/y/width/height."""
    if region is None or region == "" or region == []:
        return None
    if isinstance(region, dict):
        try:
            vals = [region["x"], region["y"], region["width"], region["height"]]
        except KeyError as exc:
            raise ValidationError(f"region dict missing {exc}") from None
    elif isinstance(region, (list, tuple)):
        vals = list(region)
    elif isinstance(region, str):
        text = region.strip().replace(" ", "")
        m = _GEOM_RE.match(text)
        if m:
            w, h, x, y = m.groups()
            vals = [x, y, w, h]
        else:
            vals = text.split(",")
    else:
        raise ValidationError(f"unsupported region {region!r}")
    if len(vals) != 4:
        raise ValidationError(f"region needs 4 numbers x,y,width,height; got {region!r}")
    try:
        x, y, w, h = (int(float(v)) for v in vals)
    except (TypeError, ValueError):
        raise ValidationError(f"region values must be integers; got {region!r}") from None
    if x < 0 or y < 0 or w <= 0 or h <= 0:
        raise ValidationError(f"region must have x,y >= 0 and positive size; got {region!r}")
    if screen and (x + w > screen[0] or y + h > screen[1]):
        raise ValidationError(f"region {x},{y},{w},{h} exceeds screen {screen[0]}x{screen[1]}")
    return Region(x, y, w, h)


@dataclass
class Window:
    id: str
    title: str
    wm_class: str
    x: int
    y: int
    width: int
    height: int
    desktop: int | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "class": self.wm_class,
            "geometry": {"x": self.x, "y": self.y, "width": self.width, "height": self.height},
            "desktop": self.desktop,
        }


def parse_wmctrl_lgx(output: str) -> list[Window]:
    """Parse `wmctrl -lGx` lines: id desktop x y w h class host title."""
    windows = []
    for line in output.splitlines():
        parts = line.split(None, 8)
        if len(parts) < 8:
            continue
        wid, desk, x, y, w, h, wm_class, _host = parts[:8]
        title = parts[8] if len(parts) > 8 else ""
        try:
            windows.append(
                Window(
                    id=f"0x{int(wid, 16):08x}",
                    title=title,
                    wm_class=wm_class,
                    x=int(x),
                    y=int(y),
                    width=int(w),
                    height=int(h),
                    desktop=int(desk),
                )
            )
        except ValueError:
            continue
    return windows


def parse_shell_vars(output: str) -> dict[str, str]:
    """Parse KEY=VALUE lines (xdotool --shell output)."""
    out = {}
    for line in output.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def parse_xprop_wm_class(output: str) -> str:
    """`WM_CLASS(STRING) = "tk", "Tk"` -> "tk.Tk" (the instance.class form wmctrl prints)."""
    if "=" not in output:
        return ""
    return ".".join(re.findall(r'"((?:[^"\\]|\\.)*)"', output.split("=", 1)[1]))


def parse_display_geometry(output: str) -> tuple[int, int]:
    """Parse `xdotool getdisplaygeometry` ("1920 1080")."""
    parts = output.split()
    if len(parts) < 2:
        raise ValidationError(f"cannot parse display geometry {output!r}")
    return int(parts[0]), int(parts[1])


def parse_xrdb_dpi(output: str) -> float | None:
    for line in output.splitlines():
        if line.lower().startswith("xft.dpi:"):
            try:
                return float(line.split(":", 1)[1])
            except ValueError:
                return None
    return None


def parse_window_ref(value) -> tuple[str, str]:
    """Return ("id", "0x…") for numeric window ids, else ("title", text)."""
    if isinstance(value, int) and not isinstance(value, bool):
        return "id", f"0x{value:08x}"
    text = str(value).strip()
    if not text:
        raise ValidationError("title_or_id must not be empty")
    if re.fullmatch(r"0x[0-9a-fA-F]+", text):
        return "id", f"0x{int(text, 16):08x}"
    if text.isdigit():
        return "id", f"0x{int(text):08x}"
    return "title", text


def png_size(path) -> tuple[int, int]:
    with open(path, "rb") as fh:
        head = fh.read(24)
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        raise ValidationError(f"{path} is not a PNG file")
    return struct.unpack(">II", head[16:24])
