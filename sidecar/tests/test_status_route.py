"""/status must answer. This is the endpoint everything depends on.

Written after shipping a 500. A helper was inserted between `@app.route('/status')`
and its view function, so Flask registered the HELPER as the view; it returns a
string or None, Flask raised TypeError, and every request 500'd. The plugin lost
every accessory and the cockpit sat on "connecting" -- a total outage from a
diagnostic that was itself supposed to make outages easier to read.

Nothing caught it. 154 tests passed: they all exercised pure functions, and not
one made a request. The Python syntax check passed too -- the file is perfectly
valid Python. Only calling the route reveals it, which is what these do.

This is backlog 1.4 ("/status contract fixture") arriving the expensive way.
"""
import json

import pytest

import pool_service as ps


@pytest.fixture
def client():
    ps.app.config['TESTING'] = True
    with ps.app.test_client() as c:
        yield c


def test_status_returns_200_and_json(client):
    """The whole failure in one assertion: the route answered 500 because the
    wrong function was registered against it."""
    r = client.get('/status')
    assert r.status_code == 200, r.get_data(as_text=True)[:400]
    assert isinstance(json.loads(r.get_data()), dict)


def test_status_view_is_the_status_function(client):
    """Pin the cause, not just the symptom. Flask maps the rule to whatever
    function the decorator wrapped -- if a helper is ever inserted between the
    decorator and the view again, this names it immediately instead of leaving
    a 500 to be diagnosed from a journal.
    """
    view = ps.app.view_functions['get_status']
    assert view.__name__ == 'get_status'


def test_status_carries_the_fields_the_plugin_reads(client):
    """A rename on this side breaks the plugin silently -- it reads these by
    name off the JSON. Not exhaustive; the ones an accessory would lose."""
    body = json.loads(client.get('/status').get_data())
    for key in ('circuits', 'pool_temp', 'air_temp', 'salt_level',
                'chlorinator_percent', 'pump_speed', 'valve_mode',
                'connected', 'bridge_wedged', 'bridge_error'):
        assert key in body, f'/status no longer returns "{key}"'


def test_bridge_error_is_none_when_nothing_is_wrong(client):
    """It exists to explain an outage; volunteering a value when there is no
    outage would put a phantom fault on the cockpit banner."""
    body = json.loads(client.get('/status').get_data())
    assert body['bridge_error'] is None


def test_every_route_has_its_own_view_function():
    """The general form of the bug: two rules sharing one view, or a rule bound
    to something that is not its handler, is how this happened. Cheap to assert
    across the whole app rather than route by route.
    """
    seen: dict[str, str] = {}
    for rule in ps.app.url_map.iter_rules():
        if rule.endpoint == 'static':
            continue
        fn = ps.app.view_functions[rule.endpoint]
        prev = seen.get(fn.__name__)
        assert prev is None or prev == rule.endpoint, (
            f'{fn.__name__} serves both {prev} and {rule.endpoint}')
        seen[fn.__name__] = rule.endpoint
