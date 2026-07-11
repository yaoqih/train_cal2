from __future__ import annotations

import sys
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import replay_manual_identity_labels as replay  # noqa: E402
import restore_manual_interface_responses as restore  # noqa: E402


def label(**overrides):
    value = {
        "manual_hook": "1",
        "line_raw": "机",
        "line": "机库线",
        "resolved_line": "机库线",
        "method": "+",
        "count": "1",
        "effective_count": "1",
        "note": "",
    }
    value.update(overrides)
    return value


def test_manual_machine_scope_excludes_engine_house() -> None:
    scopes = replay.replay_line_scopes(label())

    assert scopes == (("机南", "机走棚", "机走北"),)


def test_manual_machine_put_uses_note_for_south_segment() -> None:
    assert replay.detach_target_line(label(method="-"), "机库线") == "机走棚"
    assert replay.detach_target_line(label(method="-", note="南"), "机库线") == "机南"


def test_first_engine_house_plus_one_tracks_helper_loco() -> None:
    attach = label(line_raw="库", method="+", resolved_line="机库线")
    detach = label(manual_hook="8", line_raw="库", method="-", resolved_line="机库线")

    assert restore.is_helper_loco_attach(attach)
    assert restore.is_helper_loco_detach(detach)
    assert not restore.is_helper_loco_attach(label())
