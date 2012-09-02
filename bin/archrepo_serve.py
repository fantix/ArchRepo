#!/usr/bin/env python

import logging
from argparse import ArgumentParser


if __name__ == '__main__':
    p = ArgumentParser('archrepo_serve.py')
    p.add_argument('-v', '--verbose', choices=[0, 1, 2], type=int, default=1,
                   help='Output verbose, 0 (default) for silent, 1 for info, '
                        '2 for everything')
    args = p.parse_args()
    if args.verbose == 0:
        logging.basicConfig(level=logging.ERROR)
        logging.error('Logging is set to ERROR')
    elif args.verbose == 1:
        logging.basicConfig(level=logging.INFO)
        logging.info('Logging is set to INFO')
    elif args.verbose == 2:
        logging.basicConfig(level=logging.DEBUG)
        logging.debug('Logging is set to DEBUG')
    import cherrypy
    cherrypy.log.screen = False
    cherrypy.log.access_log.level = cherrypy.log.access_log.parent.level
    cherrypy.log.error_log.level = cherrypy.log.error_log.parent.level

    from archrepo.db_pool import buildPool
    from archrepo.repo import Processor
    from archrepo.web import ArchRepoWebServer

    pool = buildPool()
    p = Processor(pool=pool)
    p.serve()
    web_server = ArchRepoWebServer(pool)
    try:
        web_server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        p.kill()
