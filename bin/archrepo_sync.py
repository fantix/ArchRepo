#!/usr/bin/env python

import os

from archrepo import config
from archrepo.db_pool import buildPool
from archrepo.repo import Processor


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

p = Processor(pool=pool)

for path in files.difference(known):
    p._complete(path)

for path in known.difference(files):
    p._delete(path)
