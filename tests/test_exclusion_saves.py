"""Blocking a drop / ignoring a campaign must survive a settings_updated
broadcast that lands while the save is still pending (seen live: the block
confirm() dialog plus the 400ms debounce let auto-prioritize's broadcast
overwrite the new id with the server's old list, and the debounced full save
then sent the old list back). Those two lists are therefore saved right away
as their own partial update and never ride along in the full-settings save."""

import re
from pathlib import Path

APP_JS = (Path(__file__).resolve().parents[1] / "web" / "static" / "app.js").read_text(encoding="utf-8")


def _function_body(name: str) -> str:
    start = APP_JS.index(f"function {name}(")
    depth = 0
    for i in range(APP_JS.index("{", start), len(APP_JS)):
        depth += {"{": 1, "}": -1}.get(APP_JS[i], 0)
        if depth == 0:
            return APP_JS[start:i + 1]
    raise AssertionError(name)


def test_partial_save_helper_posts_only_the_given_fields():
    body = _function_body("saveSettingsPatch")
    assert "/api/settings" in body
    assert "JSON.stringify(patch)" in body


def test_blocking_the_current_drop_saves_immediately():
    body = _function_body("blacklistCurrentDrop")
    assert "saveSettingsPatch({ blacklisted_drop_ids:" in body
    assert "saveSettings()" not in body


def test_ignoring_a_campaign_saves_immediately():
    body = _function_body("toggleIgnoreCampaign")
    assert "saveSettingsPatch({ ignored_campaign_ids:" in body
    assert "saveSettings()" not in body


def test_full_save_no_longer_carries_the_exclusion_lists():
    body = _function_body("_saveSettingsNow")
    assert not re.search(r"\bblacklisted_drop_ids\s*:", body)
    assert not re.search(r"\bignored_campaign_ids\s*:", body)


def test_every_blacklist_edit_uses_the_partial_save():
    # remove button, manual add, progress-card button
    assert APP_JS.count("saveSettingsPatch({ blacklisted_drop_ids:") >= 3
