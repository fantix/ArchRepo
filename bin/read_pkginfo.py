#!/usr/bin/env python

import argparse
import sys
from gzip import GzipFile
from lzma import LZMAFile
from tarfile import TarFile
import ujson


def main(path, verify=False, format='json'):
    code = 0

    try:
        if path.endswith('.pkg.tar.gz'):
            f = GzipFile(path)
        elif path.endswith('.pkg.tar.xz'):
            f = LZMAFile(path)
        else:
            print >> sys.stderr, path, 'does not look like a package file.'
            return 1

        f = TarFile(fileobj=f)
        while True:
            info = f.next()
            if info.name == '.PKGINFO':
                break
        else:
            print >> sys.stderr, path, 'does not contain .PKGINFO'
            return 1

        if verify:
            try:
                f._load()
            except IOError:
                print >> sys.stderr, 'failed to verify', path
                code = 2

        ret = {}
        for line in f.extractfile(info).readlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if format in ('json',):
                key, value = map(str.strip, line.split('=', 1))
                if key in ret:
                    if isinstance(ret[key], list):
                        ret[key].append(value)
                    else:
                        ret[key] = [ret[key], value]
                else:
                    ret[key] = value
            else:
                print line

        if format in ('json',):
            print ujson.dumps(ret)
    except IOError:
        print >> sys.stderr, path, 'is not a valid package file.'
        return 1
    else:
        return code

if __name__ == '__main__':
    p = argparse.ArgumentParser('read_pkginfo.py')
    p.add_argument('package', metavar='PACKAGE', type=str, nargs=1,
                   help='a package file ends with .pkg.tar.gz or .pkg.tar.xz')
    p.add_argument('-f', '--format', choices=['json', 'pkginfo'],
                   default='json', help='choose the output format')
    p.add_argument('-v', '--verify', action='store_true',
                   help='verify the tarball completeness')
    args = p.parse_args()
    sys.exit(main(args.package[0], args.verify, args.format))
