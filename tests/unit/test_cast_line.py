"""The one line of instruction a campaign's cast turns into.

Pure, so it is tested here rather than through four routes: what the routes
have to get right is that they ask for it at all, which is the integration
test's job (tests/integration/test_web_campaigns.py).
"""

from __future__ import annotations

from loreline.models import CampaignPlayer
from loreline.web.generation import describe_cast


def test_no_cast_is_no_line() -> None:
    """The case that has to be right: nothing changes for a GM who never fills it in."""
    assert describe_cast([]) == ""


def test_a_full_row_names_the_character_and_the_player() -> None:
    line = describe_cast(
        [
            CampaignPlayer(player="Sara", character="Ireena"),
            CampaignPlayer(player="Tom", character="Ismark"),
        ]
    )
    assert "Ireena (played by Sara)" in line
    assert "Ismark (played by Tom)" in line
    # The fact the extraction needs: everyone else in the transcript is an NPC.
    assert "NPC" in line
    assert "spellings" in line
    # One line, because it is appended to instructions that already say what to
    # write, and a second set of instructions competes with the first.
    assert "\n" not in line


def test_a_half_filled_row_is_printed_as_the_name_it_has() -> None:
    line = describe_cast([CampaignPlayer(character="Ismark"), CampaignPlayer(player="Ben")])
    assert "Ismark, Ben" in line
    assert "played by" not in line


def test_the_order_of_the_rows_is_the_order_of_the_line() -> None:
    """Priority order everywhere, so the list reads the way the editor shows it."""
    line = describe_cast([CampaignPlayer(character="Ireena"), CampaignPlayer(character="Ismark")])
    assert line.index("Ireena") < line.index("Ismark")
