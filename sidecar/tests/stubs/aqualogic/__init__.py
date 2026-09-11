"""Minimal stand-in for the `aqualogic` package, for tests only.

rs485_bridge imports aqualogic at module load and patches it, so the daemon
cannot be imported at all without it -- and the real package is a hardware
dependency we do not want in CI. This stub exists purely to make the module
importable so its pure logic (address resolution, the bind watchdog) can be
tested. It deliberately implements nothing: any test that needs real panel
behaviour belongs on hardware, not here.
"""
