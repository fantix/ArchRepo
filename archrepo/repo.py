import gevent
import logging
import os
import pwd
import sys
import time
import ujson
from collections import defaultdict
from distutils.version import LooseVersion
from gevent import subprocess
from gevent.event import AsyncResult
from gevent.lock import RLock
from gevent.subprocess import CalledProcessError
from gevent_zeromq import zmq
from pyinotify import Event, ProcessEvent

from archrepo import config
from archrepo.utils import getZmqContext


def to_list(obj):
    if isinstance(obj, list):
        return obj
    else:
        return [obj]


class Processor(ProcessEvent):
    def my_init(self, **kwargs):
        self._started_event = AsyncResult()
        self._repo_lock = RLock()
        self._same_pkg_locks = defaultdict(RLock)
        self._ignored_move_events = set()
        self._move_events = {}

        self._pool = kwargs.get('pool')

        self._repo_dir = config.get('repository', 'path')
        self._db_name = config.get('repository', 'name') + '.db.tar.gz'
        self._verify = config.xgetbool('repository', 'verify-tarball', True)
        self._auto_rename = config.xgetbool('repository', 'auto-rename', True)
        self._command_add = config.xget('repository', 'command-add',
                                        default='repo-add')
        self._command_remove = config.xget('repository', 'command-remove',
                                        default='repo-remove')
        self._command_fuser = config.xget('repository', 'command-fuser',
                                          default='fuser')
        self._command_pkginfo = os.path.join(sys.prefix, 'bin',
                                             'read_pkginfo.py')

    def _repoAdd(self, arch, pathname):
        with self._repo_lock:
            subprocess.check_call(
                (self._command_add,
                 os.path.join(self._repo_dir, arch, self._db_name), pathname))

    def _repoRemove(self, arch, name):
        with self._repo_lock:
            subprocess.check_call(
                (self._command_remove,
                 os.path.join(self._repo_dir, arch, self._db_name), name))

    def _removeLatest(self, cur, name, arch):
        logging.info('Removing %s(%s) from repo, trying to add a lower version',
                     name, arch)
        cur.execute(
            'SELECT id, version FROM packages '
             'WHERE name=%s AND arch=%s AND enabled', (name, arch))
        latest_id, latest_version = None, None
        for _id, _ver in cur.fetchall():
            if (latest_version is None or
                LooseVersion(_ver) > latest_version):
                latest_id, latest_version = _id, LooseVersion(_ver)
        if latest_id is not None:
            cur.execute(
                'UPDATE packages SET latest=true '
                 'WHERE id=%s RETURNING file_path', (latest_id,))
            pathname, = cur.fetchone()
            try:
                self._repoAdd(arch, pathname)
            except CalledProcessError:
                logging.warning('detected missing file: ' + pathname)
                self._repoRemove(arch, name)
                gevent.spawn(self._delete, pathname)
        else:
            self._repoRemove(arch, name)

    def _checkLatest(self, cur, name, arch, pathname, pid, version):
        logging.debug('Checking if the added file %s has the latest version',
                      pathname)
        cur.execute(
            'SELECT id, version FROM packages '
             'WHERE name=%s AND arch=%s AND latest', (name, arch))
        result = cur.fetchone()
        am_latest = False
        if result:
            latest_id, latest_version = result
            if LooseVersion(version) > LooseVersion(latest_version):
                cur.execute(
                    'UPDATE packages SET latest=false '
                     'WHERE id=%s', (latest_id,))
                am_latest = True
        else:
            am_latest = True
        if am_latest:
            logging.info('Adding/Replacing %s(%s) with new version %s',
                         name, arch, version)
            cur.execute(
                'UPDATE packages SET latest=true '
                 'WHERE id=%s', (pid,))
            self._repoAdd(arch, pathname)

    def _complete(self, pathname):
        if not subprocess.call((self._command_fuser, '-s', pathname),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE):
            logging.info('Uploading ' + pathname)
            return

        partial = False
        args = (sys.executable, self._command_pkginfo, pathname)
        if self._verify:
            args += ('-v',)
        info_p = subprocess.Popen(args, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE)
        out, err = info_p.communicate()
        if info_p.returncode:
            if info_p.returncode == 2:
                partial = True
            else:
                logging.info('Ignoring, ' + err.strip())
                return

        info = ujson.loads(out)

        name = info[u'pkgname']
        version = info[u'pkgver']
        arch = info[u'arch']
        packager = info.get(u'packager')

        uploader = pwd.getpwuid(os.stat(pathname).st_uid)[0]

        if self._auto_rename and not partial:
            dest_dir = os.path.join(self._repo_dir, arch)
            if not os.path.isdir(dest_dir):
                os.mkdir(dest_dir)
            dest_path = os.path.join(dest_dir, '%s-%s-%s.pkg.tar.%s' % (
                name, version, arch, pathname.rsplit('.', 1)[-1]))
            if pathname != dest_path:
                self._ignored_move_events.add((pathname, dest_path))
                os.rename(pathname, dest_path)
                pathname = dest_path

        with self._same_pkg_locks[(name, arch)], self._pool.cursor() as cur:
            owner = None
            if packager != 'Unknown Packager':
                parts = packager.split('<', 2)
                sql = ('SELECT id FROM users '
                        'WHERE username=%(name)s OR realname=%(name)s')
                values = {'name': parts[0]}
                if len(parts) == 2:
                    sql = ' AND '.join((sql, 'email=%(email)s'))
                    values['email'] = parts[1]
                cur.execute(sql, values)
                result = cur.fetchone()
                if not result:
                    cur.execute('SELECT user_id FROM user_aliases '
                                 'WHERE alias=%s', (packager,))
                    result = cur.fetchone()
                if result:
                    owner = result[0]
            if not owner:
                cur.execute('SELECT id FROM users '
                             'WHERE username=%(name)s OR realname=%(name)s',
                        {'name': uploader})
                result = cur.fetchone()
                if not result:
                    cur.execute('SELECT user_id FROM user_aliases '
                                 'WHERE alias=%s', (uploader,))
                    result = cur.fetchone()
                if result:
                    owner = result[0]

            cur.execute(
                'SELECT id, latest, enabled FROM packages '
                 'WHERE name=%s AND arch=%s AND version=%s',
                (name, arch, version))
            result = cur.fetchone()
            fields = (
                'description', 'url', 'pkg_group', 'license', 'packager',
                'base_name', 'build_date', 'size', 'depends', 'uploader',
                'owner', 'opt_depends', 'enabled', 'file_path')
            values = (
                info.get(u'pkgdesc'), info.get(u'url'), info.get(u'group'),
                info.get(u'license'), packager, info.get(u'pkgbase', name),
                int(info.get(u'builddate', time.time())), info.get(u'size'),
                to_list(info.get(u'depend', [])), uploader, owner,
                to_list(info.get(u'optdepend', [])), not partial, pathname)
            if not result:
                logging.info('Adding new file %s(%s)', name, arch)
                cur.execute(
                    'INSERT INTO packages (name, arch, version, %s) '
                         'VALUES (%%s, %%s, %%s, %s) RETURNING id' % (
                        ', '.join(fields), ', '.join(['%s'] * len(values))),
                    (name, arch, version) + values)
                pid, = cur.fetchone()
                logging.debug('Inserted with id %s', pid)

                if not partial:
                    self._checkLatest(cur, name, arch, pathname, pid, version)
            else:
                pid, latest, enabled = result
                logging.info('Updating file #%s %s arch:%s', pid, name, arch)
                if latest and partial:
                    fields += ('latest',)
                    values += (False,)
                cur.execute(
                    'UPDATE packages SET %s, last_update=now() WHERE id=%%s' % (
                        ', '.join([x + '=%s' for x in fields]),),
                    values + (pid,))
                if latest and partial:
                    self._removeLatest(cur, name, arch)
                if not enabled and not partial:
                    self._checkLatest(cur, name, arch, pathname, pid, version)

    def _new(self, pathname):
        pass

    def _delete(self, pathname):
        if pathname.endswith('.pkg.tar.gz') or pathname.endswith('.pkg.tar.xz'):
            with self._pool.cursor() as cur:
                cur.execute(
                    'SELECT id, arch, name, latest FROM packages '
                     'WHERE file_path=%s', (pathname,))
                result = cur.fetchone()
                if result:
                    id_, arch, name, latest = result
                    with self._same_pkg_locks[(name, arch)]:
                        logging.info('Removing file record %s', pathname)
                        cur.execute(
                            'UPDATE packages SET file_path=%s, '
                            'enabled=false, '
                            'latest=false '
                            'WHERE id=%s', ('', id_,))
                        if latest:
                            self._removeLatest(cur, name, arch)

    def _move(self, src, dest):
        if (src, dest) in self._ignored_move_events:
            self._ignored_move_events.remove((src, dest))
        elif src.endswith('.pkg.tar.gz') or src.endswith('.pkg.tar.xz'):
            with self._pool.cursor() as cur:
                cur.execute(
                    'SELECT id FROM packages WHERE file_path=%s', (src,))
                result = cur.fetchone()
                if result:
                    logging.info('Updating path due to mv %s to %s', src, dest)
                    cur.execute('UPDATE packages SET file_path=%s WHERE id=%s',
                        (result[0],))

    def _modify(self, pathname):
        pass
    #        info_p = subprocess.Popen((sys.executable,
    #                                   os.path.join(sys.prefix, 'bin',
    #                                                'read_pkginfo.py'),
    #                                   pathname), stdout=subprocess.PIPE)
    #        info = ujson.loads(info_p.communicate()[0])
    #        full = int(info[u'size'])
    #        print os.stat(pathname).st_size * 100 / full, '%'

    def process_IN_MODIFY(self, event):
        if not event.dir:
            self._modify(event.pathname)

    def process_IN_CLOSE_WRITE(self, event):
        if not event.dir:
            self._complete(event.pathname)

    def process_IN_MOVED_FROM(self, event):
        if not event.dir and event.cookie is not None:
            if event.cookie in self._move_events:
                g = self._move_events[event.cookie]
                assert g._run == self._complete
                self._move(event.pathname, g.args[0])
                g.kill()
            else:
                self._move_events[event.cookie] = gevent.spawn_later(
                    1, self._delete, event.pathname)

    def process_IN_MOVED_TO(self, event):
        if not event.dir and event.cookie is not None:
            if event.cookie in self._move_events:
                g = self._move_events[event.cookie]
                assert g._run == self._delete
                self._move(g.args[0], event.pathname)
                g.kill()
            else:
                self._move_events[event.cookie] = gevent.spawn_later(
                    1, self._complete, event.pathname)

    def process_IN_CREATE(self, event):
        if not event.dir:
            self._new(event.pathname)

    def process_IN_DELETE(self, event):
        if not event.dir:
            self._delete(event.pathname)

    def process_default(self, event):
        logging.debug('Not handled %s %s', event.maskname, event.pathname)

    def serve(self):
        self._greenlet = gevent.spawn(self._serve)

    def kill(self):
        self._greenlet.kill()

    def _handle_wrapper(self, func, *args):
        try:
            func(*args)
        except Exception:
            logging.error('Error handling ZMQ message', exc_info=True)

    def handle_inotify(self, mask, cookie, _dir, pathname):
        if cookie == '':
            cookie = None
        else:
            cookie = int(cookie)
        self(Event({'mask': int(mask),
                   'pathname': pathname,
                   'dir': _dir == 'True',
                   'cookie': cookie}))

    def handle_execute(self, method, *args):
        func = getattr(self, method, None)
        if func:
            func(*args)

    def _serve(self):
        ctx = getZmqContext()
        self.socket = ctx.socket(zmq.PULL)
        try:
            try:
                self.socket.bind(config.get('repository', 'management-socket'))
            except zmq.ZMQError:
                self._started_event.set(False)
            else:
                self._started_event.set(True)
                while True:
                    parts = self.socket.recv_multipart()
                    handler = getattr(self, 'handle_' + parts[0], None)
                    if handler:
                        gevent.spawn(self._handle_wrapper, handler, *parts[1:])
        finally:
            self.socket.close()

    @property
    def serving(self):
        return self._started_event.get()


class FakeProcessor(object):
    def __init__(self):
        ctx = getZmqContext()
        self.socket = ctx.socket(zmq.PUSH)
        self.socket.connect(config.get('repository', 'management-socket'))

    def __getattr__(self, item):
        def delegate(*args):
            self.socket.send_multipart(('execute', item) + args)
        return delegate
