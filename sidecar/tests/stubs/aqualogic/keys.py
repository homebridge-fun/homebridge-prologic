"""Key names the bridge exposes at /keys. Two is enough to prove the shape."""


class _Key:
    def __init__(self, name):
        self.name = name


class _Keys:
    RIGHT = _Key('RIGHT')
    LEFT = _Key('LEFT')

    def __iter__(self):
        return iter((self.RIGHT, self.LEFT))


Keys = _Keys()
