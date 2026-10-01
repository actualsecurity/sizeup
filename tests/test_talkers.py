from sizeup.talkers import spoken, talker_prompt


def test_spoken_forms_of_todays_talker_tags():
    assert spoken("60 Ctrl OP 7") == "60 Control"
    assert spoken("77A4") == "77 Alpha 4"
    assert spoken("77M3") == "77 Medic 3"
    assert spoken("64B1") == "64 Bravo 1"
    assert spoken("E60") == "Engine 60"
    assert spoken("R40") == "Rescue 40"
    assert spoken("TL2") == "Tower Ladder 2"
    assert spoken("2398 Duty Officer") == "2398"
    assert spoken("Engine 61 Portable 1") == "Engine 61"
    assert spoken("WCDES I/O 46") is None and spoken("2391001") is None


def test_prompt_lists_each_talker_once_and_the_time_only_for_dispatch():
    # 19:49 EDT on 2026-10-01
    at = 1790898540.0
    assert talker_prompt("60 Ctrl OP 7, 77M3, 60 Ctrl OP 7") == "60 Control, 77 Medic 3."
    assert talker_prompt("60 Ctrl OP 7, 77A3", at, with_time=True) == "60 Control, 77 Alpha 3, 1949."
    assert talker_prompt("77A4", at, with_time=True) == "77 Alpha 4."       # no dispatcher: no time stamp
    assert talker_prompt("") == "" and talker_prompt("WCDES I/O 46") == ""
