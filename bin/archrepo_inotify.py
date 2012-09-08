#!/usr/bin/env python

from gevent_zeromq import zmq
from pyinotify import WatchManager, Notifier, ProcessEvent

from archrepo import config
from archrepo.utils import getZmqContext


class PrintEvents(ProcessEvent):
    def __init__(self, pevent=None, **kargs):
        super(PrintEvents, self).__init__(pevent, **kargs)
        self._socket = getZmqContext().socket(zmq.PUSH)
        self._socket.connect(config.get('repository', 'management-socket'))

    def process_default(self, event):
        self._socket.send_multipart((
            'inotify',
            str(event.mask),
            str(event.cookie) if event.mask & (0x00000040 | 0x00000080) else '',
            str(event.dir),
            event.pathname))


if __name__ == '__main__':
    path = config.get('repository', 'path')

    # watch manager instance
    wm = WatchManager()

    # notifier instance and init
    notifier = Notifier(wm, default_proc_fun=PrintEvents())

    s = """
    FLAG_COLLECTIONS = {'OP_FLAGS': {
        'IN_ACCESS'        : 0x00000001,  # File was accessed
        'IN_MODIFY'        : 0x00000002,  # File was modified
        'IN_ATTRIB'        : 0x00000004,  # Metadata changed
        'IN_CLOSE_WRITE'   : 0x00000008,  # Writable file was closed
        'IN_CLOSE_NOWRITE' : 0x00000010,  # Unwritable file closed
        'IN_OPEN'          : 0x00000020,  # File was opened
        'IN_MOVED_FROM'    : 0x00000040,  # File was moved from X
        'IN_MOVED_TO'      : 0x00000080,  # File was moved to Y
        'IN_CREATE'        : 0x00000100,  # Subfile was created
        'IN_DELETE'        : 0x00000200,  # Subfile was deleted
        'IN_DELETE_SELF'   : 0x00000400,  # Self (watched item itself)
                                          # was deleted
        'IN_MOVE_SELF'     : 0x00000800,  # Self (watched item itself) was moved
        },
                        'EVENT_FLAGS': {
        'IN_UNMOUNT'       : 0x00002000,  # Backing fs was unmounted
        'IN_Q_OVERFLOW'    : 0x00004000,  # Event queued overflowed
        'IN_IGNORED'       : 0x00008000,  # File was ignored
        },
                        'SPECIAL_FLAGS': {
        'IN_ONLYDIR'       : 0x01000000,  # only watch the path if it is a
                                          # directory
        'IN_DONT_FOLLOW'   : 0x02000000,  # don't follow a symlink
        'IN_MASK_ADD'      : 0x20000000,  # add to the mask of an already
                                          # existing watch
        'IN_ISDIR'         : 0x40000000,  # event occurred against dir
        'IN_ONESHOT'       : 0x80000000,  # only send event once
        },
                        }
    """
    # What mask to apply
    mask = 0x00000002 | 0x00000008 | 0x00000040 | 0x00000080 | 0x00000100 | 0x00000200

    wm.add_watch(path, mask, rec=True, auto_add=True)

    # Loop forever (until sigint signal get caught)
    notifier.loop()
