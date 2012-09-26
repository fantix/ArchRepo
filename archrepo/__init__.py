import logging
import os
import sys
from ConfigParser import ConfigParser


class _DefaultConfigParser(ConfigParser):
    def xget(self, section, option, raw=False, vars=None, default=None):
        if self.has_option(section, option):
            return ConfigParser.get(self, section, option, raw, vars)
        else:
            return default

    def xgetint(self, section, option, default=None):
        if self.has_option(section, option):
            return ConfigParser.getint(self, section, option)
        else:
            return default

    def xgetbool(self, section, option, default=False):
        if self.has_option(section, option):
            return ConfigParser.getboolean(self, section, option)
        else:
            return default


config = _DefaultConfigParser()
_files_read = config.read([
    os.path.join(os.environ.get('ARCHREPO_PREFIX', sys.prefix), 'etc',
                 'archrepo.ini'),
    os.path.join(os.environ.get('ARCHREPO_PREFIX', sys.prefix), 'etc',
                 'archrepo_local.ini'),
    os.path.expanduser('~/.archrepo.ini'),
    ])

for _file_name in _files_read:
    logging.debug('Reading config from ' + _file_name)

__all__ = ['config']
