"""PCFD self-dispatch (CAD): incidents, units, run cards and dispatch, next to the radio monitor.

    Cad          the service the web API and the monitor bridge call (sizeup/cad/service.py)
    store        SQLite: units, incidents, assignments, run cards, timeline, dispatch log
    runcards     call type -> recommended units
    dispatcher   dispatch text, alert tones + TTS audio
    transmit     Transmitter interface; NullTransmitter only (transmit is off in M1)
"""

from sizeup.cad.service import Cad

__all__ = ["Cad"]
