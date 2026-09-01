"""Just enough shape for rs485_bridge's import-time patching to run.

`_install_long_display_patch()` looks for a specific stub string inside the
real `AquaLogic.process` source. It won't find it here, so it prints a warning
and returns -- which is the designed behaviour for an unexpected aqualogic
version, and exactly what we want: the module imports, unpatched, and the
functions under test are untouched by any of it.
"""


class AquaLogic:
    FRAME_TYPE_LONG_DISPLAY_UPDATE = 0

    def process(self, *a, **kw):
        raise NotImplementedError('test stub')

    def _write_to_serial(self, data):
        raise NotImplementedError('test stub')

    def _send_frame(self, *a, **kw):
        raise NotImplementedError('test stub')


class States:
    HEATER_AUTO_MODE = 'HEATER_AUTO_MODE'
    HEATER_1 = 'HEATER_1'
