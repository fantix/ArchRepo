from gevent_zeromq import zmq


_zmq_context = None


def getZmqContext():
    global _zmq_context
    if _zmq_context is None:
        _zmq_context = zmq.Context()
    return _zmq_context
