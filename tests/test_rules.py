import pytest

from studio_monitor.detection.rules import RuleSet, normalize_text


def test_normalize_handles_ocr_noise():
    assert normalize_text("Your  LIVE\nhas been RESTRICTED!") == "your live has been restricted"
    assert normalize_text("You can’t go LIVE") == "you can't go live"


@pytest.mark.parametrize("text,expected", [
    ("Your LIVE has been restricted for violating our Community Guidelines.", "restriction_notice"),
    ("Warning: your LIVE may violate our Community Guidelines. Please adjust your content.", "content_warning"),
    ("Your account has been suspended. You can no longer go LIVE.", "account_suspension"),
    ("Your LIVE has ended. We ended your LIVE because of a violation.", "live_interruption"),
    ("Verify to continue. Drag the slider to fit the puzzle piece.", "verification_puzzle"),
    ("Security verification - select all images containing a bus", "verification_puzzle"),
])
def test_categories(rules, text, expected):
    m = rules.match(text)
    assert m is not None and m.key == expected


def test_priority_prefers_puzzle_over_restriction(rules):
    m = rules.match("Your LIVE has been restricted. Verify to continue by solving the puzzle.")
    assert m.key == "verification_puzzle" and m.manual_attention


def test_none_suppresses_end_live_confirmation(rules):
    assert rules.match("End LIVE? Are you sure you want to end your LIVE session ended") is None


def test_short_text_ignored(rules):
    assert rules.match("LIVE") is None


def test_no_match_on_ordinary_studio_ui(rules):
    assert rules.match("Go LIVE  Scenes  Sources  Chat  Settings  1,204 viewers  Likes 3.2K") is None


def test_ruleset_from_dict_requires_categories():
    with pytest.raises(ValueError):
        RuleSet.from_dict({"categories": {}})


def test_custom_rule_all_and_none():
    rs = RuleSet.from_dict({"min_text_chars": 3, "categories": {
        "x": {"all": ["alpha", "beta"], "none": ["gamma"]}}})
    assert rs.match("alpha and beta") is not None
    assert rs.match("alpha only") is None
    assert rs.match("alpha beta gamma") is None
