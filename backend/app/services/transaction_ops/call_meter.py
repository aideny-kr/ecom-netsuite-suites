"""Count the provider calls a read actually made, as opposed to the calls reserved for it.

The runner reserves the worst case before each read, which is what keeps a run under its
ceiling: nothing is ever sent that was not paid for first. Without a meter it also has to
keep the whole reservation, because it cannot tell what was used, and an order with no
refunds paid the same 28 calls as one with a long refund graph.

A context variable, like ``read_batch``, so readers need no new parameter: the transport
notes each send and whoever opened the meter reads the total. Child tasks copy the context
but share the one meter object, so concurrent sends inside a read are all counted.
"""

from contextlib import contextmanager
from contextvars import ContextVar

_meter: ContextVar["CallMeter | None"] = ContextVar("provider_call_meter", default=None)
_observers: ContextVar[tuple] = ContextVar("provider_call_observers", default=())


class CallMeter:
    def __init__(self):
        self.calls = 0


@contextmanager
def metered():
    meter = CallMeter()
    token = _meter.set(meter)
    try:
        yield meter
    finally:
        _meter.reset(token)


@contextmanager
def observed_calls():
    """Attribute one concurrent branch's sends without replacing the run's meter."""
    observer = CallMeter()
    token = _observers.set((*_observers.get(), observer))
    try:
        yield observer
    finally:
        _observers.reset(token)


def note_call():
    """Record one provider request, at the point it is committed to the wire."""
    meter = _meter.get()
    if meter is not None:
        meter.calls += 1
    for observer in _observers.get():
        observer.calls += 1
