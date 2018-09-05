###############################################################################
#                                                                             #
# Peekaboo Extended Email Attachment Behavior Observation Owl                 #
#                                                                             #
# daemon.py                                                                   #
###############################################################################
#                                                                             #
# Copyright (C) 2016-2018  science + computing ag                             #
#                                                                             #
# This program is free software: you can redistribute it and/or modify        #
# it under the terms of the GNU General Public License as published by        #
# the Free Software Foundation, either version 3 of the License, or (at       #
# your option) any later version.                                             #
#                                                                             #
# This program is distributed in the hope that it will be useful, but         #
# WITHOUT ANY WARRANTY; without even the implied warranty of                  #
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU           #
# General Public License for more details.                                    #
#                                                                             #
# You should have received a copy of the GNU General Public License           #
# along with this program.  If not, see <http://www.gnu.org/licenses/>.       #
#                                                                             #
###############################################################################


import os
import sys
import grp
import pwd
import stat
import logging
import SocketServer
import socket
import signal
from time import sleep
import json
from threading import Thread
from argparse import ArgumentParser
from sdnotify import SystemdNotifier
from peekaboo import _owl, __version__
from peekaboo.config import parse_config, get_config
from peekaboo.db import PeekabooDatabase
from peekaboo.toolbox.sampletools import ConnectionMap
from peekaboo.queuing import JobQueue, create_workers
from peekaboo.sample import Sample
from peekaboo.exceptions import PeekabooDatabaseError
from peekaboo.toolbox.cuckoo import Cuckoo, CuckooEmbed, CuckooApi


logger = logging.getLogger(__name__)


class SignalHandler():
    """
    Signal handler.
    
    @author: Felix Bauer
    """
    def __init__(self, timeout):
        """ register custom signal handler """
        self.timeout = timeout
        self.original_sigint_handler = signal.getsignal(signal.SIGINT)
        self.original_sigterm_handler = signal.getsignal(signal.SIGTERM)
        
        signal.signal(signal.SIGINT, self.signal_handler_int)
        signal.signal(signal.SIGTERM, self.signal_handler_int)

    def signal_handler_int(self, sig, frame):
        """ catch signal, give workers time to exit and kill """
        signal.signal(signal.SIGINT, self.signal_handler_term)
        signal.signal(signal.SIGTERM, self.signal_handler_term)
        logger.info("Shutting down. Giving workers %d seconds to stop" % self.timeout)
        # server.shutdown()
        for w in JobQueue.workers:
            w.active = False
        # wait for workers to end
        for t in range(0, self.timeout):
            w = set(JobQueue.workers)
            # check there is no existing worker
            if len(w) == 1 and None in w:
                break
            sleep(1)
        self.signal_handler_term(None, None)

    def signal_handler_term(self, sig, frame):
        """ restore original signal handlers and kill """
        signal.signal(signal.SIGTERM, self.original_sigterm_handler)
        signal.signal(signal.SIGINT, self.original_sigint_handler)
        logger.info("The End")
        os.kill(os.getpid(), signal.SIGINT)
        sys.exit(0)


class PeekabooStreamServer(SocketServer.ThreadingUnixStreamServer):
    """
    Asynchronous server.

    @author: Sebastian Deiss
    """
    def __init__(self, server_address, request_handler_cls, bind_and_activate=True):
        self.config = get_config()
        create_workers(self.config.worker_count)
        # We can only accept 2 * worker_count connections.
        self.request_queue_size = self.config.worker_count * 2
        self.allow_reuse_address = True
        
        SocketServer.ThreadingUnixStreamServer.__init__(self, server_address,
                                                        request_handler_cls,
                                                        bind_and_activate=bind_and_activate)

    def shutdown_request(self, request):
        """ Keep the connection alive until Cuckoo reports back, so the results can be send to the client. """
        # TODO: Find a better solution.
        pass

    def server_close(self):
        os.remove(self.config.sock_file)
        return SocketServer.ThreadingUnixStreamServer.server_close(self)


class PeekabooStreamRequestHandler(SocketServer.StreamRequestHandler):
    """
    Request handler used by PeekabooStreamServer to handle analysis requests.

    @author: Sebastian Deiss
    """
    def handle(self):
        """
        Handles an analysis request. This is expected to be a JSON structure
        containing the path of the directory / file to analyse. Structure:

        [ { "full_name": "<path>",
            "name_declared": ...,
            ... },
          { ... },
          ... ]

        The maximum buffer size is 16 KiB, because JSON incurs some bloat.
        """
        self.request.sendall('Hallo das ist Peekaboo\n\n')
        request = self.request.recv(1024 * 16).rstrip()

        try:
            parts = json.loads(request)
        except:
            self.request.sendall('FEHLER: Ungueltiges JSON.')
            logger.error('Invalid JSON in request.')
            return

        if type(parts) not in (list, tuple):
            self.request.sendall('FEHLER: Ungueltiges Datenformat.')
            logger.error('Invalid data structure.')
            return

        for_analysis = []
        for part in parts:
            if not part.has_key('full_name'):
                self.request.sendall('FEHLER: Unvollstaendige Datenstruktur.')
                logger.error('Incomplete data structure.')
                return

            path = part['full_name']
            logger.info("Got run_analysis request for %s" % path)
            if not os.path.exists(path):
                self.request.sendall('FEHLER: Pfad existiert nicht oder '
                        'Zugriff verweigert.')
                logger.error('Path does not exist or no permission '
                        'to access it.')
                return

            if not os.path.isfile(path):
                self.request.sendall('FEHLER: Eingabe ist keine Datei.')
                logger.error('Input is not a file')
                return

            sample = Sample(path, part, self.request)
            for_analysis.append(sample)
            logger.debug('Created sample %s' % sample)

        # introduced after an issue where results were reported
        # before all files could be added.
        for sample in for_analysis:
            ConnectionMap.add(self.request, sample)
            JobQueue.submit(sample, self.__class__)


def run():
    """ Runs the Peekaboo daemon. """
    arg_parser = ArgumentParser(
        description='Peekaboo Extended Email Attachment Behavior Observation Owl'
    )
    arg_parser.add_argument(
        '-c', '--config',
        action='store',
        required=False,
        default=os.path.join('./peekaboo.conf'),
        help='The configuration file for Peekaboo.'
    )
    arg_parser.add_argument(
        '-d', '--debug',
        action='store_true',
        required=False,
        default=False,
        help="Run Peekaboo in debug mode regardless of what's specified in the configuration."
    )
    arg_parser.add_argument(
        '-D', '--daemon',
        action='store_true',
        required=False,
        default=False,
        help='Run Peekaboo in daemon mode (suppresses the logo to be written to STDOUT).'
    )
    args = arg_parser.parse_args()

    if not args.daemon:
        print(_owl)
    else:
        print('Starting Peekaboo %s.' % __version__)

    # read configuration
    if not os.path.isfile(args.config):
        print('Failed to read config, files does not exist.') # logger doesn't exist here
        sys.exit(1)
    config = parse_config(args.config)

    SignalHandler(600)

    # Check if CLI arguments override the configuration
    if args.debug:
        config.change_log_level('DEBUG')

    # Log the configuration options if we are in debug mode
    if config.log_level == logging.DEBUG:
        logger.debug(config.__str__())

    # establish a connection to the database
    try:
        db_con = PeekabooDatabase(config.db_url)
        config.add_db_con(db_con)
    except PeekabooDatabaseError as e:
        logging.exception(e)
        sys.exit(1)
    except Exception as e:
        logger.critical('Failed to establish a connection to the database.')
        logger.exception(e)
        sys.exit(1)

    # Import debug module if we are in debug mode
    debugger = None
    if config.use_debug_module:
        from peekaboo.debug import PeekabooDebugger
        debugger = PeekabooDebugger()
        debugger.start()

    if os.getuid() == 0:
        logger.warning('Peekaboo should not run as root.')
        # drop privileges to user
        os.setgid(grp.getgrnam(config.group)[2])
        os.setuid(pwd.getpwnam(config.user)[2])
        # set $HOME to the users home directory
        # (VirtualBox must access the configs)
        os.environ['HOME'] = pwd.getpwnam(config.user)[5]
        logger.info("Dropped privileges to user %s and group %s"
                    % (config.user, config.group))
        logger.debug('$HOME is ' + os.environ['HOME'])

    # write PID file
    pid = str(os.getpid())
    with open(config.pid_file, "w") as pidfile:
        pidfile.write("%s\n" % pid)

    systemd = SystemdNotifier()
    # Try three times to start SocketServer
    for i in range(0, 3):
        try:
            server = PeekabooStreamServer(config.sock_file, PeekabooStreamRequestHandler)
            break
        except socket.error, msg:
            logger.warning("SocketServer couldn't start (%i)" % i)
    if not server:
        logger.error('Fatal: Couldn\'t initialise Peekaboo Server')
        sys.exit(1)

    runner = Thread(target=server.serve_forever)
    runner.daemon = True

    try:
        runner.start()
        logger.info('Peekaboo server is listening on %s' % server.server_address)

        os.chmod(config.sock_file, stat.S_IWOTH | stat.S_IREAD |
                                   stat.S_IWRITE | stat.S_IRGRP |
                                   stat.S_IWGRP | stat.S_IWOTH)

        # If this dies Peekaboo dies, since this is the main thread. (legacy)
        if config.cuckoo_mode == "embed":
            cuckoo = CuckooEmbed(config.interpreter, config.cuckoo_exec)
        # otherwise it's the new API method and default
        else:
            cuckoo = CuckooApi(config.cuckoo_url)
        config.add_cuckoo_obj(cuckoo)
        systemd.notify("READY=1")
        cuckoo.do()
    except Exception as e:
        logger.exception(e)
    finally:
        server.shutdown()
        if debugger is not None:
            debugger.shut_down()


if __name__ == '__main__':
    run()
