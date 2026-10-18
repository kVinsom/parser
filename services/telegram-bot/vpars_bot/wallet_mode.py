"""Runtime checkout policy shared by callbacks, onboarding and the durable worker."""

from contextlib import contextmanager
from weakref import WeakKeyDictionary

_modes = WeakKeyDictionary()


def button_checkout(sessions):
    return _modes.get(sessions) == "bot"


@contextmanager
def checkout_mode(sessions, mode):
    previous = _modes.get(sessions)
    _modes[sessions] = mode
    try:
        yield
    finally:
        if previous is None:
            _modes.pop(sessions, None)
        else:
            _modes[sessions] = previous
