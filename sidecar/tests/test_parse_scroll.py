"""Tests for the pure LCD-frame parsers.

These are the first automated tests in the project. They cover the layer
where almost every bug this project has actually shipped has lived: turning
panel LCD text into state.

Frame strings here are written to match the regexes in pool_service. Where a
frame is a guess rather than something captured from real hardware it is
marked, because a test written against invented text can pass against fiction
-- see docs/testing-strategy.md, "Tier B".
"""
import pool_service as ps


# ── parse_ac_scroll: individual readings ────────────────────────────────────

def test_empty_frame_yields_nothing():
    """An empty dict means 'this frame told us nothing' -- the caller relies
    on that to avoid touching last_update."""
    assert ps.parse_ac_scroll('') == {}
    assert ps.parse_ac_scroll('some unrelated text') == {}


def test_scroll_readings():
    assert ps.parse_ac_scroll('Pool Temp  78')['pool_temp'] == 78
    assert ps.parse_ac_scroll('Air Temp  91')['air_temp'] == 91
    assert ps.parse_ac_scroll('Salt Level  3200')['salt_level'] == 3200
    assert ps.parse_ac_scroll('Pool Chlorinator  30%')['chlorinator_percent'] == 30
    assert ps.parse_ac_scroll('Spa Chlorinator  5%')['spa_chlorinator_percent'] == 5


def test_negative_temperature_parses():
    """The pattern allows a leading '-'; a freezing air temp must not be
    silently dropped."""
    assert ps.parse_ac_scroll('Air Temp  -4')['air_temp'] == -4


def test_both_vsp_active_slot_formats():
    """Two confirmed panel formats report the running slot. Both were seen on
    hardware; missing the second is a bug this project has already had."""
    assert ps.parse_ac_scroll('Filter Speed 50% Speed2')['vsp_active_slot'] == 2
    assert ps.parse_ac_scroll('Filter On:Spd3')['vsp_active_slot'] == 3


def test_slot_percent_is_a_merge_mapping():
    """vsp_slot_pct comes back as {slot: pct} to be merged, never to replace
    the whole dict -- otherwise reading slot 1 would erase slots 2-4."""
    assert ps.parse_ac_scroll('Filter Speed1  90%')['vsp_slot_pct'] == {1: 90}
    assert ps.parse_ac_scroll('Filter Speed4  50%')['vsp_slot_pct'] == {4: 50}


# ── heater state: the body-routing rule ─────────────────────────────────────

def test_prefixed_heater_state_routes_by_prefix():
    assert ps.parse_ac_scroll('Spa Heater1 Auto Control') == {'spa_heater_enabled': True}
    assert ps.parse_ac_scroll('Pool Heater1 Manual Off') == {'pool_heater_enabled': False}


def test_unprefixed_heater_state_routes_by_active_body():
    """An unprefixed line refers to whichever body is active -- which is the
    reason parse_ac_scroll takes valve_mode at all."""
    assert ps.parse_ac_scroll('Heater1 Auto Control', 'spa') == {'spa_heater_enabled': True}
    assert ps.parse_ac_scroll('Heater1 Auto Control', 'pool') == {'pool_heater_enabled': True}


def test_unprefixed_heater_state_defaults_to_pool_when_mode_unknown():
    assert ps.parse_ac_scroll('Heater1 Auto Control', None) == {'pool_heater_enabled': True}


# ── setpoints ───────────────────────────────────────────────────────────────

def test_setpoint_implies_enabled():
    """The °F only appears when the heater is enabled, so seeing it also
    confirms Auto."""
    got = ps.parse_ac_scroll('Pool Heater1  85\xb0F')
    assert got == {'pool_setpoint_f': 85, 'pool_heater_enabled': True}


def test_setpoint_overrides_an_earlier_manual_off():
    """Order is load-bearing: the setpoint branch runs after the heater-state
    branch and deliberately wins."""
    got = ps.parse_ac_scroll('Spa Heater1 Manual Off 99\xb0F')
    assert got['spa_setpoint_f'] == 99
    assert got['spa_heater_enabled'] is True


def test_implausible_setpoints_are_rejected():
    """Sanity bounds (40-110 °F) stop a stray number being taken as a
    setpoint."""
    assert ps.parse_ac_scroll('Pool Heater1  999\xb0F') == {}
    assert ps.parse_ac_scroll('Pool Heater1  10\xb0F') == {}


# ── purity ──────────────────────────────────────────────────────────────────

def test_parser_does_not_touch_shared_state():
    """The whole point of the extraction: parsing must have no side effects."""
    before = (ps.state.pool_temp, ps.state.salt_level, ps.state.last_update)
    ps.parse_ac_scroll('Pool Temp  78')
    ps.parse_ac_scroll('Salt Level  3200')
    assert (ps.state.pool_temp, ps.state.salt_level, ps.state.last_update) == before


# ── LED nibble decode (already pure before the refactor) ────────────────────

def test_led_nibble_states():
    """3=absent, 4=off, 5=on, 6=blink. Independently confirmed against
    homebridge-aqua-connect-lite's identical table."""
    assert ps._ac_led_nibbles('C') == ('off', 'absent')   # 0x43 -> 4,3
    assert ps._ac_led_nibbles('D') == ('off', 'off')      # 0x44 -> 4,4
    assert ps._ac_led_nibbles('E') == ('off', 'on')       # 0x45 -> 4,5
    assert ps._ac_led_nibbles('U') == ('on', 'on')        # 0x55 -> 5,5


def test_decode_led_line_is_pure_and_shaped():
    out = ps._decode_ac_led('UUUUUU')
    assert out['pool_mode'] == 'on'
    for key in ('spa_mode', 'spillover_mode', 'filter', 'lights',
                'heater', 'aux1', 'aux2'):
        assert key in out


def test_decode_led_rejects_short_input():
    """A truncated line must not raise -- it returns nothing to fold in."""
    assert ps._decode_ac_led('') == {}
    assert ps._decode_ac_led('UU') == {}


# ── the state fold, including the quirk the refactor preserved ──────────────

def test_apply_folds_parsed_fields_into_state():
    ps.state.pool_temp = None
    ps._apply_ac_scroll_to_state('Pool Temp  78')
    assert ps.state.pool_temp == 78


def test_apply_merges_slot_percentages_rather_than_replacing():
    ps.state.vsp_slot_pct.clear()
    ps.state.vsp_slot_pct[2] = 60
    ps._apply_ac_scroll_to_state('Filter Speed1  90%')
    assert ps.state.vsp_slot_pct == {2: 60, 1: 90}


def test_a_frame_that_says_nothing_leaves_last_update_alone():
    ps.state.last_update = 12345.0
    ps._apply_ac_scroll_to_state('unrelated text')
    assert ps.state.last_update == 12345.0


def test_pump_startup_alone_does_not_bump_last_update():
    """Documented quirk, preserved verbatim by the parse/apply split: the
    original set pump_startup without touching last_update. Probably an
    oversight, but this test pins current behaviour so a future fix is a
    deliberate change rather than an accident. See docs/backlog.md.
    """
    ps.state.last_update = 12345.0
    parsed = ps.parse_ac_scroll('Filter Speed  40%')
    assert set(parsed) <= {'pump_startup', 'pump_speed', 'vsp_active_slot'}
    if set(parsed) == {'pump_startup'}:
        ps._apply_ac_scroll_to_state('Filter Speed  40%')
        assert ps.state.last_update == 12345.0
