"""youtube-auto watchdog code mapping.

The five codes come from saglik-kontrol.js. If that script gains a sixth, these tests
document that it must surface as unknown rather than being silently downgraded.
"""
from app.main import _SEVERITY_ORDER, _YT_CODE_MAP
from app import enums


def test_all_five_saglik_kontrol_codes_are_mapped():
    assert set(_YT_CODE_MAP) == {
        "STALE_LOCK", "JOB_RETRYING", "UPLOAD_BLOCKED",
        "DAILY_NOT_STARTED", "DAILY_NOT_PUBLISHED",
    }


def test_mapped_event_types_are_all_alertable():
    # A mapping that produces a non-alertable type would silently swallow the alert.
    for event_type, _ in _YT_CODE_MAP.values():
        assert event_type in enums.ALERTABLE_EVENTS, event_type


def test_severity_ordering_picks_the_worst_code():
    codes = ["DAILY_NOT_PUBLISHED", "JOB_RETRYING"]
    mapped = [_YT_CODE_MAP[c] for c in codes]
    worst = max((m[1] for m in mapped), key=_SEVERITY_ORDER.index)
    assert worst is enums.Severity.HIGH  # not the MEDIUM of DAILY_NOT_PUBLISHED


def test_severity_order_is_ascending():
    assert _SEVERITY_ORDER[0] is enums.Severity.INFO
    assert _SEVERITY_ORDER[-1] is enums.Severity.CRITICAL
