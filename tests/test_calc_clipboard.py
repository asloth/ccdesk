"""Lossless clipboard snapshots, including non-text targets and failures before ownership."""
from types import SimpleNamespace as NS

import pytest

from ccdesk.calc_clipboard import snapshot


def atom(name):
    return NS(name=lambda: name)


def data(payload, bits=8, kind="UTF8_STRING"):
    return NS(get_length=lambda: len(payload), get_data=lambda: payload,
              get_format=lambda: bits, get_data_type=lambda: atom(kind))


def test_snapshot_preserves_bytes_formats_and_actual_types():
    formats = {"UTF8_STRING": data(b"before"), "image/png": data(b"\x00\xff\x89png", kind="image/png"),
               "custom": data(b"\x00\x00\xff\xff", bits=16, kind="INTEGER")}
    clip = NS(wait_for_targets=lambda: (True, [atom(n) for n in ["TARGETS", "TIMESTAMP", "MULTIPLE", "SAVE_TARGETS", *formats]]),
              wait_for_contents=lambda a: formats[a.name()])
    assert snapshot(clip) == {"UTF8_STRING": (b"before", 8, "UTF8_STRING"),
                              "image/png": (b"\x00\xff\x89png", 8, "image/png"),
                              "custom": (b"\x00\x00\xff\xff", 16, "INTEGER")}


def test_empty_clipboard_is_supported():
    assert snapshot(NS(wait_for_targets=lambda: (False, []))) == {}


@pytest.mark.parametrize("contents", [None, NS(get_length=lambda: -1)])
def test_unsaved_target_refuses_the_whole_snapshot(contents):
    clip = NS(wait_for_targets=lambda: (True, [atom("image/png")]), wait_for_contents=lambda _: contents)
    with pytest.raises(ValueError, match="cannot save clipboard target"):
        snapshot(clip)


def test_oversized_clipboard_is_refused_before_ownership():
    clip = NS(wait_for_targets=lambda: (True, [atom("text")]),
              wait_for_contents=lambda _: data(b"x" * (16 * 1024 * 1024 + 1)))
    with pytest.raises(ValueError, match="too large"):
        snapshot(clip)


def test_too_many_targets_are_refused_before_ownership():
    clip = NS(wait_for_targets=lambda: (True, [atom(str(i)) for i in range(129)]),
              wait_for_contents=lambda _: data(b"x"))
    with pytest.raises(ValueError, match="too large"):
        snapshot(clip)
