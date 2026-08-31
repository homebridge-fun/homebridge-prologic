"""The novel-frame-shape ledger.

This runs on every LCD frame, so the tests care as much about it being
harmless as about it being correct: a bug here must cost a test fixture, never
a dropped frame.
"""
import pool_service as ps


def _reset():
    with ps._frame_shapes_lock:
        ps._frame_shapes.clear()


def test_shape_collapses_readings_but_keeps_the_wording():
    assert ps.frame_shape('Pool Temp  78') == ps.frame_shape('Pool Temp  79')
    assert ps.frame_shape('Pool Temp 78') == 'Pool Temp <N>'
    assert ps.frame_shape('Pool Temp 78') != ps.frame_shape('Air Temp 78')


def test_shape_strips_wbon_markup():
    """The panel wraps highlighted values in HTML; two frames differing only by
    markup are the same shape."""
    assert (ps.frame_shape('Pool Heater1 <span class="WBON">85</span>\xb0F')
            == ps.frame_shape('Pool Heater1 85\xb0F'))


def test_shape_matches_the_harvester_implementation():
    """The sidecar and scripts/harvest_frames.py must agree on the dedupe key,
    or the harvester will re-report shapes the corpus already has."""
    import importlib.util
    import pathlib
    path = pathlib.Path(__file__).parents[2] / 'scripts' / 'harvest_frames.py'
    spec = importlib.util.spec_from_file_location('harvest_frames', path)
    hf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hf)
    for frame in ('Pool Temp  78', 'Filter Speed 50% Speed2',
                  'Super Chlorinate 12:34 remaining',
                  'Pool Heater1 <span class="WBON">85</span>\xb0F', ''):
        assert ps.frame_shape(frame) == hf.shape(frame), frame


def test_records_a_new_shape_once_and_counts_repeats():
    _reset()
    ps._note_frame_shape('Pool Temp  78')
    ps._note_frame_shape('Pool Temp  79')   # same shape, different reading
    ps._note_frame_shape('Air Temp  91')
    with ps._frame_shapes_lock:
        shapes = dict(ps._frame_shapes)
    assert set(shapes) == {'Pool Temp <N>', 'Air Temp <N>'}
    assert shapes['Pool Temp <N>']['count'] == 2
    # The first example is kept verbatim -- that is what the corpus needs.
    assert shapes['Pool Temp <N>']['text'] == 'Pool Temp  78'


def test_empty_and_blank_frames_are_ignored():
    _reset()
    for junk in ('', None, '   ', '<span></span>'):
        ps._note_frame_shape(junk)
    with ps._frame_shapes_lock:
        assert ps._frame_shapes == {}


def test_ledger_is_capped():
    """A frame format we fail to normalise must not grow this without bound."""
    _reset()
    for i in range(ps._FRAME_SHAPES_MAX + 25):
        ps._note_frame_shape(f'unique frame {chr(65 + i % 26)}{i}x')
    with ps._frame_shapes_lock:
        assert len(ps._frame_shapes) <= ps._FRAME_SHAPES_MAX


def test_a_bad_frame_never_raises_into_the_frame_path():
    """The whole point: this is corpus bookkeeping. Losing a shape is
    immaterial; raising into text_updated would not be."""
    _reset()

    class Hostile:
        def __bool__(self):
            raise RuntimeError('boom')

    ps._note_frame_shape(Hostile())          # must not propagate
    ps._note_frame_shape(object())           # not a string
    ps._note_frame_shape(12345)


def test_text_updated_still_records_frames_end_to_end():
    """Wired into the real capture path, not just callable in isolation."""
    _reset()
    cap = ps.LcdCapture(maxhist=5)
    cap.text_updated('Salt Level  3200')
    with ps._frame_shapes_lock:
        assert 'Salt Level <N>' in ps._frame_shapes


# ── transport artifacts (found on real hardware) ────────────────────────────

def test_trailing_nul_does_not_split_a_frame_into_two_shapes():
    """The real panel emitted 'Salt Level 3000 PPM' both with and without a
    trailing NUL, and the ledger recorded them as two separate shapes -- 53
    of one and 1734 of the other. \\s does not match \\x00, so the NUL survived
    normalisation. Every frame type was being double-counted."""
    assert (ps.frame_shape('     Salt Level    3000 PPM      ')
            == ps.frame_shape('     Salt Level    3000 PPM      \x00'))
    assert ps.frame_shape('Salt Level 3000 PPM\x00') == 'Salt Level <N> PPM'


def test_newlines_survive_because_the_lcd_is_two_lines():
    """Control chars are stripped, but \\n separates the LCD's two lines and is
    collapsed to a space by the whitespace rule, not deleted outright."""
    assert ps.frame_shape('Pool Temp 78\nAir Temp 91') == 'Pool Temp <N> Air Temp <N>'


def test_stored_example_is_cleaned_so_fixtures_are_deterministic():
    """Which raw variant arrives first is arbitrary; the corpus should not
    depend on it."""
    _reset()
    ps._note_frame_shape('Salt Level  3000 PPM\x00')
    with ps._frame_shapes_lock:
        assert '\x00' not in ps._frame_shapes['Salt Level <N> PPM']['text']


def test_a_ledger_written_before_the_fix_is_merged_on_load(tmp_path):
    """Otherwise the NUL-split shapes linger forever as phantom entries."""
    import json
    old = {
        'Salt Level <N> PPM': {'first_seen': 100.0, 'last_seen': 200.0,
                               'count': 53, 'text': 'Salt Level 3000 PPM'},
        'Salt Level <N> PPM \x00': {'first_seen': 90.0, 'last_seen': 300.0,
                                    'count': 1734,
                                    'text': 'Salt Level 3000 PPM\x00'},
    }
    path = tmp_path / 'frame_shapes.json'
    path.write_text(json.dumps(old))
    saved = ps._FRAME_SHAPES_PATH
    try:
        ps._FRAME_SHAPES_PATH = str(path)
        _reset()
        ps._load_frame_shapes()
        with ps._frame_shapes_lock:
            shapes = dict(ps._frame_shapes)
    finally:
        ps._FRAME_SHAPES_PATH = saved
    assert list(shapes) == ['Salt Level <N> PPM']
    entry = shapes['Salt Level <N> PPM']
    assert entry['count'] == 53 + 1734        # counts combined, not lost
    assert entry['first_seen'] == 90.0        # earliest sighting wins
    assert entry['last_seen'] == 300.0        # latest sighting wins


def test_degree_symbol_variants_are_one_shape():
    """Hardware decodes the degree symbol as '_' on some frames and the real
    symbol on others, splitting the sensor and cell-diagnostic screens in two."""
    assert (ps.frame_shape('  Cell Temp Sensor          76_F        ')
            == ps.frame_shape('  Cell Temp Sensor          76\xb0F        '))
    assert (ps.frame_shape('  -25.31V  -5.81A   76_F   2900 PPM  ')
            == ps.frame_shape('  -25.31V  -5.81A   76\xb0F   2900 PPM  '))


def test_shape_inherits_norm_handling_of_cursor_bytes():
    """_norm strips the masked highlight bytes that decorate a fresh frame.
    frame_shape delegates to it rather than reimplementing, which is how the
    trailing-NUL split happened in the first place."""
    assert (ps.frame_shape('\x03\x03  Super Chlorinate   Off  ')
            == ps.frame_shape('  Super Chlorinate   Off  '))
    assert (ps.frame_shape(') Pool Chlorinator 50%')
            == ps.frame_shape('Pool Chlorinator 50%'))


def test_full_ledger_evicts_a_one_off_rather_than_refusing_new_frames():
    """Mis-decoded serial noise arrives as an endless stream of single-sighting
    shapes. Refusing new entries once full would let it lock out real frames."""
    _reset()
    with ps._frame_shapes_lock:
        for i in range(ps._FRAME_SHAPES_MAX):
            ps._frame_shapes[f'noise <N> {i}'] = {
                'first_seen': 0, 'last_seen': 0, 'count': 1, 'text': f'n{i}'}
    ps._note_frame_shape('Filter T1-all 07:00A to 08:00A')
    with ps._frame_shapes_lock:
        assert 'Filter T<N>-all <N>:<N>A to <N>:<N>A' in ps._frame_shapes
        assert len(ps._frame_shapes) <= ps._FRAME_SHAPES_MAX


def test_corroborated_shapes_are_not_evicted():
    """If everything retained has been seen more than once, keep it -- a
    repeatedly-observed frame outranks an unseen new one."""
    _reset()
    with ps._frame_shapes_lock:
        for i in range(ps._FRAME_SHAPES_MAX):
            ps._frame_shapes[f'real <N> {i}'] = {
                'first_seen': 0, 'last_seen': 0, 'count': 9, 'text': f'r{i}'}
    ps._note_frame_shape('Something Brand New 12')
    with ps._frame_shapes_lock:
        assert len(ps._frame_shapes) == ps._FRAME_SHAPES_MAX


def test_blinking_clock_colon_is_one_shape():
    """The idle screen's colon blinks, so it alternates between '12:49P' and
    '12 49P'. Highest-traffic screen on the panel; it was counted twice."""
    assert (ps.frame_shape('       Monday              12 49P       ')
            == ps.frame_shape('       Monday              12:49P       '))


def test_clock_normalisation_leaves_timer_ranges_alone():
    """The timer screens carry real times that must not be merged together."""
    t1 = ps.frame_shape('   Filter T1-all      07:00A to 08:00A  ')
    t3 = ps.frame_shape('   Filter T3-all       9:30P to 10:30P  ')
    assert t1 != t3
    assert 'to' in t1


# ── what counts as variable, and what deliberately does not ─────────────────

def test_day_names_collapse_because_we_never_read_them():
    """Otherwise the panel's busiest screen accumulates seven copies a week."""
    shapes = {ps.frame_shape(f'       {d}              12:49P       ')
              for d in ('Monday', 'Tuesday', 'Wednesday', 'Thursday',
                        'Friday', 'Saturday', 'Sunday')}
    assert shapes == {'<DAY> <N>:<N>P'}


def test_states_the_parser_must_discriminate_stay_distinct():
    """The rule: tokenise values the parser ignores, keep apart what it reads.

    Collapsing these would hide whether both states are handled -- which is
    precisely how the 0.8.6 Super Chlorinate bug stayed invisible, where one
    state parsed and the other silently did not.
    """
    for a, b in (('Beeper Enabled', 'Beeper Disabled'),
                 ('Flow Switch Flow', 'Flow Switch No Flow'),
                 ('Pool Heater1 Auto Control', 'Pool Heater1 Manual Off'),
                 ('Filter T1-all 07:00A to 08:00A',
                  'Filter T1-all 07:00P to 08:00P')):
        assert ps.frame_shape(a) != ps.frame_shape(b), f'{a} vs {b}'


def test_every_numeric_reading_collapses_regardless_of_value():
    """The digit rule is universal, not time-specific: a screen is identified
    by its wording, never by the reading it happens to be showing."""
    for a, b in (('Salt Level 3000 PPM', 'Salt Level 3100 PPM'),
                 ('Filter Speed 50% Speed2', 'Filter Speed 75% Speed3'),
                 ('Pool Chlorinator 30%', 'Pool Chlorinator 65%'),
                 ('Filter T1-Spd1 90%', 'Filter T1-Spd1 45%'),
                 ('Pool Heater1 85\xb0F', 'Pool Heater1 92\xb0F'),
                 ('Pool Temp 78', 'Pool Temp 81'),
                 ('Main Software Revision 4.46', 'Main Software Revision 5.10'),
                 ('Super Chlorinate 24 hours', 'Super Chlorinate 8 hours')):
        assert ps.frame_shape(a) == ps.frame_shape(b), f'{a} vs {b}'


def test_sign_is_part_of_the_reading_not_the_screen():
    """The salt cell reverses polarity to self-clean, so its diagnostic screen
    alternates sign -- one screen that was producing two shapes. Same for a
    sub-zero air temperature."""
    assert (ps.frame_shape('  -25.31V   -5.81A   76\xb0F   2900 PPM  ')
            == ps.frame_shape('   25.31V    5.81A   76\xb0F   2900 PPM  '))
    assert ps.frame_shape('Air Temp  -4') == ps.frame_shape('Air Temp  78')


def test_hyphens_that_are_not_signs_survive():
    """Only a '-' at a token boundary followed by a digit is a sign. Menu
    labels and the '--- Off ---' placeholders must be left alone, or genuinely
    different screens would start colliding."""
    assert ps.frame_shape('Filter T1-all 07:00A to 08:00A') \
        == 'Filter T<N>-all <N>:<N>A to <N>:<N>A'
    assert ps.frame_shape('Spa-all --- Off ---') == 'Spa-all --- Off ---'
    assert ps.frame_shape('Filter T1-Spd1 90%') == 'Filter T<N>-Spd<N> <N>%'


# ── partial-render detection (harvester heuristic) ──────────────────────────

def _harvester():
    import importlib.util
    import pathlib
    path = pathlib.Path(__file__).parents[2] / 'scripts' / 'harvest_frames.py'
    spec = importlib.util.spec_from_file_location('harvest_frames', path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_mid_repaint_fragments_are_recognised():
    hf = _harvester()
    for frag, whole in (('Pool Chlorinator', 'Pool Chlorinator 30%'),
                        ('Filter T2-all 08:00A to', 'Filter T2-all 8:00A to 09:30P'),
                        ('Filter T2-all to 09:30P', 'Filter T2-all 8:00A to 09:30P'),
                        ('Spa-all --- Off', 'Spa-all --- Off ---')):
        assert hf.is_partial_of(hf.shape(frag), hf.shape(whole)), frag


def test_a_different_screen_is_not_a_fragment_just_for_sharing_tokens():
    """The idle clock's tokens are a subsequence of the Set Day and Time
    screen, but they are different screens -- and the clock is the most-shown
    one on the panel. A fragment always starts with the same label, because
    the label is drawn first and persists through the repaint.
    """
    hf = _harvester()
    idle = hf.shape('       Monday               3 33P       ')
    menu = hf.shape('  Set Day and Time    Monday     3:34P  ')
    assert idle != menu
    assert not hf.is_partial_of(idle, menu)


def test_a_fragment_is_matched_against_every_shape_not_just_its_bucket():
    """'Pool Chlorinator' with no value is a fragment of 'Pool Chlorinator
    30%', which the parser reads perfectly and so lands in a different bucket.
    Comparing only within a bucket left the header reported as a parser bug.
    """
    hf = _harvester()
    ledger = ['  Pool Chlorinator                      ',
              '  Pool Chlorinator          30%         ',
              '  Super Chlorinate          Off         ',
              '  Super Chlorinate        24 hours      ',
              '   Display Light                        ',
              '  Display Software     Revision         ']
    shapes = [hf.shape(t) for t in ledger]

    def is_frag(sh):
        return any(sh != o and hf.is_partial_of(sh, o) for o in shapes)

    assert is_frag(hf.shape(ledger[0])), 'header should be a fragment'
    # Everything else is a screen in its own right, including pairs that share
    # a label -- collapsing those would hide a state the parser must read.
    for t in ledger[1:]:
        assert not is_frag(hf.shape(t)), t
