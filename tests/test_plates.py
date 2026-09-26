from app.plates import PlateVoter, normalize


def test_common_formats():
    assert normalize("LEB1234") == ("LEB-1234", True)
    assert normalize("BKA-123") == ("BKA-123", True)
    assert normalize("AZF 52") == ("AZF-52", True)


def test_registration_year_and_suffix_letter():
    assert normalize("LEA204060") == ("LEA-20-4060", True)
    assert normalize("LE165471A") == ("LE-16-5471A", True)


def test_lookalike_characters_are_fixed_by_position():
    # 5 in the letter part is an S, O in the number part is a 0
    assert normalize("LEB12O4") == ("LEB-1204", True)
    assert normalize("5EB1234") == ("SEB-1234", True)


def test_garbage_is_not_valid():
    assert normalize("12")[1] is False
    assert normalize("")[1] is False


def test_voter_prefers_repeated_confident_reading():
    v = PlateVoter()
    for _ in range(5):
        v.add("LEA4060", 0.9, 400)
    v.add("LEA4068", 0.95, 400)
    text, conf = v.result()
    assert text == "LEA-4060"
    assert 0 < conf <= 1
    assert v.readings == 6
