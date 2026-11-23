import vinted_fast as fast

NEWEST = 3_218_549_074
NEW = {"feedback_count": 0.0, "feedback_reputation": 0.0}


def reason(stats, user_id, price, category="menswear", rows=500):
    return fast._fresh_account_reason(stats, user_id, NEWEST, rows, price, category)


def test_real_scam_sellers_blocked():
    # 11-21 Hamilton $120 (graciegrahambm) and 11-22 Purple Label $85 (57ptyco88af)
    assert "never-ship" in reason(NEW, 3_218_182_983, 120, "watches")
    assert "never-ship" in reason(NEW, 3_218_549_074, 85)
    # one 1-star review does not clear a days-old account
    assert reason({"feedback_count": 1.0, "feedback_reputation": 0.2}, 3_218_000_000, 73)


def test_honest_sellers_pass():
    assert reason({"feedback_count": 13.0, "feedback_reputation": 1.0}, 3_184_052_981, 40, "watches") is None
    assert reason(NEW, 3_208_846_780, 85) is None  # 0 reviews but ~3 weeks old
    assert reason(NEW, 3_218_500_000, 12) is None  # cheap polo from a new seller
    assert reason({"feedback_count": 2.0, "feedback_reputation": 1.0}, 3_218_500_000, 85) is None


def test_fails_open_without_data():
    assert reason(NEW, 3_218_500_000, 120, "watches", rows=10) is None
    assert reason({}, 3_218_500_000, 120, "watches") is None
    assert reason(NEW, None, 120, "watches") is None


def test_missing_rating_fails_open():
    assert reason({"feedback_count": 3.0}, 3_218_500_000, 85) is None


def test_age_helper():
    assert fast._account_age_days(3_218_549_074 - 414_000, NEWEST, 500) == 1
    assert fast._account_age_days(1, NEWEST, 10) is None
