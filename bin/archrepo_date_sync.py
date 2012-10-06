#!/usr/bin/env python

import datetime
import gevent
import os

from archrepo import config
from archrepo.db_pool import buildPool


files = set()
def _walker(arg, dirname, fnames):
    for name in fnames:
        if name.endswith('.pkg.tar.gz') or name.endswith('.pkg.tar.xz'):
            _file = os.path.abspath(os.path.join(dirname, name))
            if not os.path.islink(_file):
                files.add(_file)
os.path.walk(config.get('repository', 'path'), _walker, None)

known = set()
pool = buildPool()
to_id = {}
with pool.cursor() as cur:
    cur.execute('SELECT id, file_path FROM packages')
    for _id, path in cur.fetchall():
        if path:
            path = path.encode('utf-8')
            known.add(path)
            to_id[path] = _id

def sync():
    for path in files.intersection(known):
        mtime = os.path.getmtime(path)
        with pool.cursor() as cur:
            cur.execute('UPDATE packages SET last_update=%s WHERE id=%s',
                        (datetime.datetime.utcfromtimestamp(mtime), to_id[path]))

gevent.spawn(sync).join()
