"""Tests for the pad bridge's bind-address handling.

These cover the failure that produced them. The bridge binds the pad's tailnet
address so only authenticated tailnet peers can reach the API. That address was
resolved ONCE, at install time, and written as a literal into
/etc/pool-bridge.env -- so when the node was renumbered from the Tailscale
console, the daemon kept a socket on an address that no longer existed on the
interface. Every connection was refused, and systemd went on reporting the
service `active`. Nothing on the pad noticed; the only evidence was the far end
failing to poll.

The daemon is imported against a stub `aqualogic` (sidecar/tests/stubs) because
it patches that library at module load and the real one is a hardware
dependency. None of the logic under test touches it.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / 'stubs'))

import pytest                                                   # noqa: E402

import rs485_bridge as rb                                        # noqa: E402


# ── resolving --listen ──────────────────────────────────────────────────────

def test_explicit_address_is_passed_through_untouched(monkeypatch):
    """Someone who pinned an address meant it. Resolution must not override
    it -- silently substituting a different address would be worse than the
    staleness this feature exists to fix."""
    monkeypatch.setattr(rb, 'iface_ipv4', lambda *a, **k: '100.64.0.9')
    assert rb.resolve_listen('100.64.0.1:8899') == ('100.64.0.1', 8899)
    assert rb.resolve_listen('0.0.0.0:8899') == ('0.0.0.0', 8899)


def test_tailnet_alias_resolves_to_the_interface_address(monkeypatch):
    monkeypatch.setattr(rb, 'iface_ipv4', lambda *a, **k: '100.64.0.9')
    for spec in ('tailnet:8899', 'tailscale:8899', 'tailscale0:8899',
                 'TAILNET:8899'):
        assert rb.resolve_listen(spec) == ('100.64.0.9', 8899), spec


def test_a_renumber_is_picked_up_at_the_next_start(monkeypatch):
    """The whole point: resolution happens per-start, so restarting the daemon
    after a renumber is the entire fix."""
    addrs = iter(['100.64.0.1', '100.64.0.2'])
    monkeypatch.setattr(rb, 'iface_ipv4', lambda *a, **k: next(addrs))
    assert rb.resolve_listen('tailnet:8899')[0] == '100.64.0.1'
    assert rb.resolve_listen('tailnet:8899')[0] == '100.64.0.2'


def test_missing_port_defaults(monkeypatch):
    monkeypatch.setattr(rb, 'iface_ipv4', lambda *a, **k: '100.64.0.9')
    assert rb.resolve_listen('tailnet') == ('100.64.0.9', 8899)


def test_it_waits_for_the_interface_rather_than_failing_immediately(monkeypatch):
    """At boot the daemon can start before Tailscale has an address. Resolving
    once and giving up would trade the renumber bug for a reboot bug -- which
    is how the address came to be frozen at install time in the first place.
    """
    calls = {'n': 0}

    def later(*a, **k):
        calls['n'] += 1
        return '100.64.0.7' if calls['n'] >= 3 else None

    monkeypatch.setattr(rb, 'iface_ipv4', later)
    monkeypatch.setattr(rb.time, 'sleep', lambda *_: None)
    assert rb.resolve_listen('tailnet:8899', wait_s=30) == ('100.64.0.7', 8899)
    assert calls['n'] == 3


def test_it_gives_up_loudly_when_the_interface_never_appears(monkeypatch):
    """Refusing to start beats binding something unintended: falling back to
    0.0.0.0 would silently expose the API to the whole LAN, which is the one
    outcome the tailnet bind exists to prevent."""
    monkeypatch.setattr(rb, 'iface_ipv4', lambda *a, **k: None)
    monkeypatch.setattr(rb.time, 'sleep', lambda *_: None)
    with pytest.raises(SystemExit) as e:
        rb.resolve_listen('tailnet:8899', wait_s=0)
    assert 'tailscale0' in str(e.value)


# ── the watchdog that makes a dead socket visible ───────────────────────────

class _Exited(Exception):
    """Stands in for os._exit, which pytest cannot catch."""


def _run_watch_once(monkeypatch, bound, current):
    monkeypatch.setattr(rb, 'iface_ipv4', lambda *a, **k: current)
    monkeypatch.setattr(rb.time, 'sleep', lambda *_: None)

    def boom(code):
        raise _Exited(code)

    monkeypatch.setattr(rb.os, '_exit', boom)
    return rb.watch_bound_address(bound, interval_s=0)


def test_watchdog_exits_when_the_bound_address_leaves_the_interface(monkeypatch):
    """Exiting is what makes this self-healing: Restart=on-failure brings the
    daemon back and startup resolves the new address. Staying up is the bug --
    that is the state that reported `active` while refusing every connection.
    """
    with pytest.raises(_Exited) as e:
        _run_watch_once(monkeypatch, '100.64.0.1', '100.64.0.2')
    assert e.value.args[0] != 0, 'must be a FAILURE exit or systemd will not restart it'


def test_watchdog_tolerates_a_momentarily_unreadable_interface(monkeypatch):
    """`ip` failing is not evidence the address is gone. Exiting on a transient
    read error would restart the daemon for no reason."""
    calls = {'n': 0}

    def flaky(*a, **k):
        calls['n'] += 1
        if calls['n'] > 3:
            raise _Exited(0)      # stop the loop; nothing should have exited
        return None               # unreadable

    monkeypatch.setattr(rb, 'iface_ipv4', flaky)
    monkeypatch.setattr(rb.time, 'sleep', lambda *_: None)
    monkeypatch.setattr(rb.os, '_exit', lambda c: (_ for _ in ()).throw(
        AssertionError(f'exited {c} on a transient read failure')))
    with pytest.raises(_Exited):
        rb.watch_bound_address('100.64.0.1', interval_s=0)
