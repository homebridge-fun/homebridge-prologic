"""Which light standard sits on which relay is per-installation config.

It used to be hardcoded to the maintainer's wiring (pool = Hayward ColorLogic
on LIGHTS, spa = Pentair IntelliBrite on AUX_1). These tests pin that those
values are still the DEFAULTS, and that changing them actually re-points the
derived lookups the light code reads.
"""
import pytest

import pool_service as ps


@pytest.fixture(autouse=True)
def restore_light_config():
    """Light config is module-global; put it back so tests don't leak."""
    saved = {b: dict(c) for b, c in ps.LIGHT_CFG_BY_BODY.items()}
    yield
    for b, c in saved.items():
        ps.LIGHT_CFG_BY_BODY[b] = dict(c)
    ps._apply_light_types()


def test_defaults_match_the_reference_installation():
    ps._apply_light_types()
    assert ps.LIGHT_CFG_BY_BODY['pool']['type'] == 'colorlogic'
    assert ps.LIGHT_CFG_BY_BODY['spa']['type'] == 'intellibrite'
    assert ps.LIGHT_CIRCUITS == {'pool': 'LIGHTS', 'spa': 'AUX_1'}
    assert ps.LIGHT_MECHANIC == {'pool': 'relative', 'spa': 'absolute'}


def test_every_standard_declares_what_the_light_code_needs():
    for name, spec in ps.LIGHT_TYPES.items():
        assert spec['mechanic'] in ('relative', 'absolute'), name
        assert isinstance(spec['offset'], int), name
        assert spec['programs'], name
        assert spec['label'], name


def test_changing_the_standard_repoints_mechanic_and_programs():
    ps.LIGHT_CFG_BY_BODY['spa']['type'] = 'colorlogic'
    ps._apply_light_types()
    assert ps.LIGHT_MECHANIC['spa'] == 'relative'
    assert ps.LIGHT_PROGRAMS_BY_BODY['spa'] is ps.LIGHT_PROGRAMS_POOL


def test_changing_the_relay_repoints_the_circuit():
    ps.LIGHT_CFG_BY_BODY['pool']['circuit'] = 'AUX_2'
    ps._apply_light_types()
    assert ps.LIGHT_CIRCUITS['pool'] == 'AUX_2'


def test_two_lights_can_share_a_standard():
    """Nothing requires one of each -- plenty of installs have two ColorLogic
    lights, which the old hardcoding made impossible to express."""
    for b in ('pool', 'spa'):
        ps.LIGHT_CFG_BY_BODY[b]['type'] = 'colorlogic'
    ps._apply_light_types()
    assert ps.LIGHT_MECHANIC == {'pool': 'relative', 'spa': 'relative'}


def test_unknown_standard_is_ignored_rather_than_crashing():
    """A bad value must not take the sidecar down -- that would kill every
    accessory, not just the light."""
    before = dict(ps.LIGHT_MECHANIC)
    ps.LIGHT_CFG_BY_BODY['spa']['type'] = 'omnidirect'   # real, but unsupported
    ps._apply_light_types()
    assert ps.LIGHT_MECHANIC == before
