import abc
import gevent
import logging
import os
import pwd
import sys
import time
import ujson
from collections import defaultdict
from datetime import datetime
from distutils.version import LooseVersion
from gevent import subprocess
from gevent.event import AsyncResult
from gevent.lock import RLock
from gevent.lock import Semaphore
from gevent_zeromq import zmq
from pyinotify import Event, ProcessEvent

from archrepo import config
from archrepo.utils import getZmqContext


arches = ('i686', 'x86_64')


def to_list(obj):
    if isinstance(obj, list):
        return obj
    else:
        return [obj]


class OwnerFinder(object):
    __metaclass__ = abc.ABCMeta

    def __call__(self, packager, uploader):
        packager = packager.lower()
        uploader = uploader.lower()
        owner = None
        if packager != 'Unknown Packager':
            parts = packager.split('<', 2)
            name = parts[0].strip()
            email = None
            if len(parts) == 2:
                email = parts[1].rstrip('>').strip()
            owner = self.fromUsers(name, email)
            if not owner:
                owner = self.fromAliases(name)
        if not owner:
            owner = self.fromUsers(uploader)
            if not owner:
                owner = self.fromAliases(uploader)
        return owner

    @abc.abstractmethod
    def fromUsers(self, name, email=None):
        pass

    @abc.abstractmethod
    def fromAliases(self, alias):
        pass


class OwnerFinderInAll(OwnerFinder):
    def __init__(self, cursor):
        self.cursor = cursor

    def fromUsers(self, name, email=None):
        sql = ('SELECT id FROM users '
                'WHERE lower(username)=%(name)s OR lower(realname)=%(name)s')
        values = {'name': name}
        if email is not None:
            sql = ' AND '.join((sql, 'lower(email)=%(email)s'))
            values['email'] = email
        self.cursor.execute(sql, values)
        result = self.cursor.fetchone()
        if result:
            return result[0]

    def fromAliases(self, alias):
        self.cursor.execute('SELECT user_id FROM user_aliases '
                             'WHERE lower(alias)=%s', (alias,))
        result = self.cursor.fetchone()
        if result:
            return result[0]


class OwnerFinderForMe(OwnerFinder):
    def __init__(self, uid, username, realname, email, aliases):
        self.uid = uid
        self.username = username and username.lower()
        self.realname = realname and realname.lower()
        self.email = email and email.lower()
        self.aliases = {x.lower() for x in aliases}

    def fromUsers(self, name, email=None):
        if name == self.username or name == self.realname:
            return self.uid
        if email is not None and email == self.email:
            return self.uid

    def fromAliases(self, alias):
        if alias in self.aliases:
            return self.uid


class Processor(ProcessEvent):
    def my_init(self, **kwargs):
        self._started_event = AsyncResult()
        self._repo_lock = defaultdict(RLock)
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
        self._command_pkginfo = os.path.join(
            os.environ.get('ARCHREPO_PREFIX', sys.prefix), 'bin',
            'read_pkginfo.py')
        self._semaphore = Semaphore(
            config.xgetint('repository', 'concurrent-jobs', default=256))

    def _repoAddInternal(self, arch, pathname):
        with self._repo_lock[arch]:
            subprocess.check_call(
                (self._command_add,
                 os.path.join(self._repo_dir, arch, self._db_name), pathname))

    def _repoRemoveInternal(self, arch, name):
        with self._repo_lock[arch]:
            subprocess.check_call(
                (self._command_remove,
                 os.path.join(self._repo_dir, arch, self._db_name), name))

    def _repoRemove(self, arch, name):
        if arch == 'any':
            for _arch in arches:
                self._repoRemoveInternal(_arch, name)
        else:
            self._repoRemoveInternal(arch, name)

    def _repoAdd(self, arch, pathname):
        if arch == 'any':
            for _arch in arches:
                _target_dir = os.path.join(self._repo_dir, _arch)
                _target_link = os.path.join(_target_dir,
                                            os.path.basename(pathname))
                _link = True
                if os.path.exists(_target_link):
                    if os.path.samefile(pathname, _target_link):
                        _link = False
                    else:
                        os.unlink(_target_link)
                elif os.path.lexists(_target_link):
                    os.unlink(_target_link)
                if _link:
                    os.symlink(os.path.relpath(pathname, _target_dir),
                               _target_link)
                self._repoAddInternal(_arch, _target_link)
        else:
            self._repoAddInternal(arch, pathname)

    def _unlinkForAny(self, arch, pathname):
        if arch == 'any':
            for _arch in arches:
                _path = os.path.join(self._repo_dir, _arch,
                                     os.path.basename(pathname))
                if os.path.islink(_path):
                    os.unlink(_path)

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
            if os.path.exists(pathname):
                self._repoAdd(arch, pathname)
            else:
                logging.warning('detected missing file: ' + pathname)
                self._unlinkForAny(arch, pathname)
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
        if pathname.rstrip('.lck').endswith('.db.tar.gz'):
            return

        if os.path.islink(pathname):
            return

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
        mtime = datetime.utcfromtimestamp(os.path.getmtime(pathname))

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
            owner = OwnerFinderInAll(cur)(packager, uploader)

            cur.execute(
                'SELECT id, latest, enabled FROM packages '
                 'WHERE name=%s AND arch=%s AND version=%s',
                (name, arch, version))
            result = cur.fetchone()
            fields = (
                'description', 'url', 'pkg_group', 'license', 'packager',
                'base_name', 'build_date', 'size', 'depends', 'uploader',
                'owner', 'opt_depends', 'enabled', 'file_path', 'last_update')
            values = (
                info.get(u'pkgdesc'), info.get(u'url'), info.get(u'group'),
                info.get(u'license'), packager, info.get(u'pkgbase', name),
                int(info.get(u'builddate', time.time())), info.get(u'size'),
                to_list(info.get(u'depend', [])), uploader, owner,
                to_list(info.get(u'optdepend', [])), not partial, pathname,
                mtime)
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
                    'UPDATE packages SET %s WHERE id=%%s' % (
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
                            'UPDATE packages '
                               'SET file_path=%s, '
                                   'enabled=false, '
                                   'latest=false '
                             'WHERE id=%s', ('', id_,))
                        if latest:
                            self._removeLatest(cur, name, arch)
                    self._unlinkForAny(arch, pathname)

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
    #                                   os.path.join(
    #               os.environ.get('ARCHREPO_PREFIX', sys.prefix), 'bin',
    #                                                'read_pkginfo.py'),
    #                                   pathname), stdout=subprocess.PIPE)
    #        info = ujson.loads(info_p.communicate()[0])
    #        full = int(info[u'size'])
    #        print os.stat(pathname).st_size * 100 / full, '%'

    def _autoAdopt(self, uid):
        with self._pool.cursor() as cur:
            cur.execute('SELECT username, realname, email FROM users '
                         'WHERE id=%s', (uid,))
            result = cur.fetchone()
            if not result:
                return
            username, realname, email = result
            cur.execute('SELECT alias FROM user_aliases '
                         'WHERE user_id=%s', (uid,))
            result = cur.fetchall()
            aliases = [x[0] for x in result]
            finder = OwnerFinderForMe(uid, username, realname, email, aliases)
            cur.execute('SELECT id, packager, uploader FROM packages '
                         'WHERE owner IS NULL')
            for pid, packager, uploader in cur.fetchall():
                owner = finder(packager, uploader)
                if owner is not None:
                    cur.execute('UPDATE packages SET owner=%s WHERE id=%s',
                                (owner, pid))

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
            with self._semaphore:
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
