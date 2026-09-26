from app.gate_rules import check_pw, clean_plate, hash_pw, same_plate


def test_same_plate_ignores_year_and_dash():
    assert same_plate("LEA-20-4060", "LEA-4060")
    assert same_plate("LE-16-5471A", "LE-5471A")


def test_same_plate_allows_one_ocr_mistake():
    assert same_plate("LES-5033", "LES-0033")
    assert not same_plate("LES-5033", "LES-0038")


def test_short_plates_must_match_exactly():
    assert same_plate("AZF-52", "AZF-52")
    assert not same_plate("AZF-52", "AZF-53")
    assert not same_plate(None, "AZF-52")


def test_clean_plate_normalises_typed_input():
    assert clean_plate("leb 1234") == "LEB-1234"


def test_password_hash_round_trip():
    h = hash_pw("gate-secret-1")
    assert check_pw("gate-secret-1", h)
    assert not check_pw("wrong", h)
    assert not check_pw("x", "garbage")
