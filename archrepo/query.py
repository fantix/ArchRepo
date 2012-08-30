import logging
import time
from gevent import Greenlet
from gevent import spawn
from gevent.event import AsyncResult
from gevent.lock import Semaphore
from gevent.pool import Group, Pool
from gevent.queue import Queue, Empty

from archrepo import config


class Killed(Exception):
    def __init__(self, result):
        self.result = result
        super(Killed, self).__init__()


class ReusableCursor(Greenlet):
    def __init__(self, pool, key, sql, values):
        super(ReusableCursor, self).__init__(self.work)
        self.pool = pool
        self.key = self._formatted_info = key
        self.sql = sql
        self.values = values
        self.offset = 0
        self.queue = Queue()
        self._count = AsyncResult()
        self.last_access = time.time()
        self.idle = False
        self.listeners = []
        self.window = config.xgetint('web', 'query-reusable-window', 30)

    @property
    def count(self):
        return self._count.get()

    def work(self):
        try:
            with self.pool.connection() as conn:
                cur = conn.cursor('_cur')
                cur.execute(self.sql, self.values)
                logging.debug(cur.query)
                cur_tmp = conn.cursor()
                cur_tmp.execute('MOVE ALL FROM _cur')
                self._count.set(int(cur_tmp.statusmessage.split()[-1]))
                cur_tmp.close()
                cur.scroll(0, 'absolute')
                while True:
                    if not self.queue.qsize():
                        self.idle = True
                        for l in self.listeners:
                            spawn(l.onIdle, self)
                    result, limit, offset = self.queue.get(timeout=self.window)
                    self.idle = False
                    if limit is None:
                        raise Killed(result)
                    if self.offset != offset:
                        cur.scroll(offset, 'absolute')
                    data = cur.fetchmany(limit)
                    self.offset = offset + limit
                    result.set(data)
                    self.last_access = time.time()
        except Empty:
            pass
        except Killed, k:
            k.result.set()
        finally:
            self.queue = None

    def fetch(self, limit, offset):
        result = AsyncResult()
        self.queue.put((result, limit, offset))
        return result.get()

    def close(self):
        result = AsyncResult()
        self.queue.put((result, None, None))
        result.get()

    def addListener(self, listener):
        self.listeners.append(listener)

    def __hash__(self):
        return hash(self.key)


class CountingSemaphore(object):
    def __init__(self, *args, **kwargs):
        self._semaphore = Semaphore(*args, **kwargs)
        self.waiting = 0

    def acquire(self, *args, **kwargs):
        try:
            self.waiting += 1
            return self._semaphore.acquire(*args, **kwargs)
        finally:
            self.waiting -= 1

    def __getattr__(self, name):
        return getattr(self._semaphore, name)


class CursorPool(Pool):
    def __init__(self, size=16, greenlet_class=None):
        Group.__init__(self)
        self.size = size
        self.greenlet_class = greenlet_class or ReusableCursor
        self._semaphore = CountingSemaphore(size)

    def getCursor(self, db_pool, key, sql, values):
        to_close, _time = None, None
        for g in self.greenlets:
            if g.key == key:
                logging.debug('Reusing cursor in %s', self.__class__.__name__)
                return g
            if g.idle and (_time is None or g.last_access < _time):
                to_close, _time = g, g.last_access
        if self.full() and to_close is not None:
            logging.debug('Killing idle cursor in %s', self.__class__.__name__)
            to_close.close()
        ret = self.spawn(db_pool, key, sql, values)
        ret.addListener(self)
        return ret

    def onIdle(self, cursor):
        if self._semaphore.waiting:
            cursor.close()


class SubCursorPool(CursorPool):
    def __init__(self, parent, size=1):
        super(SubCursorPool, self).__init__(size, parent.getCursor)
