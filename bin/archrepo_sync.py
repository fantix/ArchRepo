#!/usr/bin/env python

import gevent
import os

from archrepo import config
from archrepo.db_pool import buildPool
from archrepo.repo import Processor
from archrepo.repo import FakeProcessor


files = set()
def _walker(arg, dirname, fnames):
    for name in fnames:
        if name.endswith('.pkg.tar.gz') or name.endswith('.pkg.tar.xz'):
            files.add(os.path.abspath(os.path.join(dirname, name)))
os.path.walk(config.get('repository', 'path'), _walker, None)

known = set()
pool = buildPool()
with pool.cursor() as cur:
    cur.execute('SELECT file_path FROM packages')
    for path, in cur.fetchall():
        known.add(path)

local = True
p = Processor(pool=pool)
p.serve()

if p.serving:
    print 'Repo processor is up'
else:
    local = False
    print 'Connecting to Arch Repo management socket...'
    p = FakeProcessor()

def sync():
    for path in files.difference(known):
        print 'Adding new file to repo', path
        p._complete(path)

    for path in known.difference(files):
        print 'Deleting package from repo', path
        p._delete(path)

    if local:
        p.kill()

gevent.spawn(sync).join()
