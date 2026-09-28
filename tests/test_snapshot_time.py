"""generated_at ageing must not depend on the reader's timezone.

Regression: the warmer (GitHub runner, UTC) stamped a naive datetime.now()
and the app aged it against its own naive datetime.now(). On an IST machine a
snapshot written a minute ago read as 5.5 h old, so Command Centre threw every
fresh Top Picks snapshot away and ran a live scan instead.
"""
import datetime

from utils.snapshot_time import parse_snapshot_timestamp, snapshot_age_seconds, snapshot_timestamp

_UTC = datetime.timezone.utc
_IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def test_fresh_stamp_is_fresh():
    assert 0 <= snapshot_age_seconds(snapshot_timestamp()) < 5


def test_aware_stamp_from_other_timezone_ages_correctly():
    ten_min_ago_ist = (datetime.datetime.now(_UTC) - datetime.timedelta(minutes=10)).astimezone(_IST)
    assert abs(snapshot_age_seconds(ten_min_ago_ist.isoformat()) - 600) < 5


def test_legacy_naive_stamp_is_read_as_utc():
    naive_utc = (datetime.datetime.now(_UTC) - datetime.timedelta(minutes=1)).replace(tzinfo=None)
    assert abs(snapshot_age_seconds(naive_utc.isoformat()) - 60) < 5


def test_unparseable_stamps():
    for bad in (None, "", "unknown", 123):
        assert parse_snapshot_timestamp(bad) is None
        assert snapshot_age_seconds(bad) is None
