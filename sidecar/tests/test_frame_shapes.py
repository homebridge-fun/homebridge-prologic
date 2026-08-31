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
