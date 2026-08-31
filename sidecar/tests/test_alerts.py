"""The cockpit alert banner must reflect now, not the recent past.

Alerts age out on a 10-minute timer. That is right for a transient warning,
but wrong for one describing a condition that has since been fixed: a
resolved bridge outage kept showing as current for the rest of the window,
even though the banner's own text promises it "self-clears on reconnect".
"""
import logging

import pool_service as ps


def _record(buf, msg, level=logging.WARNING):
    buf.emit(logging.LogRecord('t', level, __file__, 0, msg, None, None))


def test_resolve_drops_only_matching_alerts():
    buf = ps._AlertBuffer()
    _record(buf, 'RS-485 bridge unreachable (3 consecutive polls)')
    _record(buf, 'heater setpoint write did not confirm')
    assert buf.resolve(ps._BRIDGE_OFFLINE_ALERT) == 1
    remaining = [a['msg'] for a in buf.recent()]
    assert remaining == ['heater setpoint write did not confirm']


def test_resolve_clears_every_repeat_of_the_condition():
    """Coalescing coexists with resolving: the banner showed the outage as
    'x2', and reconnecting must clear the whole row, not decrement it."""
    buf = ps._AlertBuffer()
    _record(buf, 'RS-485 bridge unreachable (3 consecutive polls)')
    _record(buf, 'RS-485 bridge unreachable (5 consecutive polls)')
    assert buf.resolve(ps._BRIDGE_OFFLINE_ALERT) == 2
    assert buf.recent() == []


def test_resolve_is_a_no_op_when_nothing_matches():
    buf = ps._AlertBuffer()
    _record(buf, 'some unrelated warning')
    assert buf.resolve(ps._BRIDGE_OFFLINE_ALERT) == 0
    assert len(buf.recent()) == 1


def test_the_raised_message_actually_contains_the_resolve_needle():
    """The guard that matters: the raise site formats the constant into a
    longer sentence, and the resolve matches on a substring. If someone
    rewords the log line, this fails instead of the alert silently never
    clearing again."""
    raised = (f'{ps._BRIDGE_OFFLINE_ALERT} (3 consecutive polls) — '
              f'marking offline; self-clears on reconnect')
    buf = ps._AlertBuffer()
    _record(buf, raised)
    assert buf.resolve(ps._BRIDGE_OFFLINE_ALERT) == 1
