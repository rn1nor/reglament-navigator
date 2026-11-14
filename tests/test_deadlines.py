import datetime as dt

from navigator.qa.deadlines import compute, extract_rules, is_working_day, shift_working_days


def test_extract_before_rule():
    rules = extract_rules("Работник подаёт заявление не позднее чем за 14 календарных дней до начала отпуска.")
    assert len(rules) == 1
    r = rules[0]
    assert (r.offset, r.unit, r.direction) == (14, "calendar_days", "before")
    assert r.anchor.startswith("начала отпуска")
    compute(r, dt.date(2026, 12, 1))
    assert r.computed == dt.date(2026, 11, 17)


def test_working_days_skip_weekends_and_holidays():
    assert not is_working_day(dt.date(2026, 11, 4))  # День народного единства
    assert not is_working_day(dt.date(2026, 11, 7))  # суббота
    # 5 рабочих дней до понедельника 9 ноября 2026: 6 (пт), 5 (чт), 3 (вт), 2 (пн), 30 октября (пт); 4 ноября праздник
    assert shift_working_days(dt.date(2026, 11, 9), 5, "before") == dt.date(2026, 10, 30)


def test_within_rule_and_words():
    rules = extract_rules("Приказ издаётся в течение трех рабочих дней со дня подачи заявления.")
    assert rules and (rules[0].offset, rules[0].unit, rules[0].direction) == (3, "working_days", "after")
    compute(rules[0], dt.date(2026, 11, 6))  # пятница -> пн 9, вт 10, ср 11
    assert rules[0].computed == dt.date(2026, 11, 11)


def test_fixed_date_rule():
    rules = extract_rules("Заявления принимаются не позднее 15 сентября 2026 года.")
    assert any(r.fixed_date == dt.date(2026, 9, 15) for r in rules)
