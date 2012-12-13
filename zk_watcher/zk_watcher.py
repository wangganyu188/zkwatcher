#!/usr/bin/python
#
# Copyright 2012 Nextdoor.com, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Daemon that monitors a set of services and updates a ServiceRegistry
with their status.

The purpose of this script is to monitor a given 'service' on a schedule
defined by 'refresh' and register or de-register that service with an Apache
ZooKeeper instance.

The script reads in a config file (default /etc/zk/config.cfg) and parses each
section. Each section begins with a header that defines the service name for
logging purposes, and then contains several config options that tell us how to
monitor the service. Eg:

  [memcache]
  cmd: pgrep memcached
  refresh: 30
  service_port: 11211
  zookeeper_path: /services/prod-uswest1-mc
  zookeeper_data: foo=bar

Copyright 2012 Nextdoor Inc.
References: http://code.activestate.com/recipes/66012/
Advanced Programming in the Unix Environment by W. Richard Stevens
"""

__author__ = 'matt@nextdoor.com (Matt Wise)'

from sys import stdout, stderr
import re
import optparse
import socket
import subprocess
import threading
import time
import signal
import ConfigParser
import logging
import logging.handlers
import os
import sys

# Get our ServiceRegistry class
from ndServiceRegistry import KazooServiceRegistry as ServiceRegistry

# Our default variables
from version import __version__ as VERSION

# Defaults
LOG = '/var/log/zk_watcher.log'
ZOOKEEPER_SESSION_TIMEOUT_USEC = 300000  # microseconds
ZOOKEEPER_URL = 'localhost:2181'

# This global variable is used to trigger the service stopping/starting...
RUN_STATE = True

# First handle all of the options passed to us
usage = 'usage: %prog <options>'
parser = optparse.OptionParser(usage=usage, version=VERSION,
                               add_help_option=True)
parser.set_defaults(verbose=True)
parser.add_option('-c', '--config', dest='config',
                  default='/etc/zk/config.cfg',
                  help='override the default config file (/etc/zk/config.cfg)')
parser.add_option('-s', '--server', dest='server', default=ZOOKEEPER_URL,
                  help='server address (default: localhost:2181')
parser.add_option('-v', '--verbose', action='store_true', dest='verbose',
                  default=False,
                  help='verbose mode')
parser.add_option('-l', '--syslog', action='store_true', dest='syslog',
                  default=False,
                  help='log to syslog')
(options, args) = parser.parse_args()


class WatcherDaemon(threading.Thread):
    """The main daemon process.

    This is the main object that defines all of our major functions and
    connection information."""

    LOGGER = 'WatcherDaemon'

    def __init__(self, server, config_file, verbose=False):
        """Initilization code for the main WatcherDaemon.

        Set up our local logger reference, and pid file locations."""
        # Initiate our thread
        super(WatcherDaemon, self).__init__()

        self._watchers = []

        # Bring in our configuration options
        self._config = ConfigParser.ConfigParser()
        self._config.read(config_file)

        self.log = logging.getLogger(self.LOGGER)
        self.log.info('WatcherDaemon %s' % VERSION)

        # Get a logger for ndServiceRegistry and set it to be quiet
        nd_log = logging.getLogger('ndServiceRegistry')

        # Set up our threading environment
        self._event = threading.Event()

        # These threads can die with prejudice. Make sure that any time the
        # python interpreter exits, we exit immediately
        self.setDaemon(True)

        # Create our ServiceRegistry object
        self._sr = ServiceRegistry(server=server, lazy=True)

        # Start up
        self.start()

    def run(self):
        """Start up all of the worker threads and keep an eye on them"""

        # Create our individual watcher threads from the config sections
        for service in self._config.sections():
            w = Watcher(registry=self._sr,
                        service=service,
                        service_port=self._config.get(service, 'service_port'),
                        command=self._config.get(service, 'cmd'),
                        path=self._config.get(service, 'zookeeper_path'),
                        data={},
                        refresh=self._config.get(service, 'refresh'))
            self._watchers.append(w)

        # Now, loop. Wait for a death signal
        while True and not self._event.is_set():
            self._event.wait(1)

        # At this point we must be exiting. Kill off our above threads
        for w in self._watchers:
            w.stop()

    def stop(self):
        self._event.set()


class Watcher(threading.Thread):
    """Monitors a particular service definition."""

    LOGGER = 'WatcherDaemon.Watcher'

    def __init__(self, registry, service, service_port, command, path, data,
                 name=socket.getfqdn(), refresh=15):
        """Initialize the object and begin monitoring the service."""
        # Initiate our thread
        super(Watcher, self).__init__()

        self._sr = registry
        self._service = service
        self._service_port = service_port
        self._command = command
        self._refresh = int(refresh)
        self._path = path
        self._data = data
        self._fullpath = '%s/%s:%s' % (path, name, service_port)
        self.log = logging.getLogger('%s.%s' % (self.LOGGER, self._service))
        self.log.debug('Initializing...')

        self._event = threading.Event()
        self.setDaemon(True)
        self.start()

    def run(self):
        """Monitors the supplied service, and keeps it registered.

        We loop every second, checking whether or not we need to run our
        check. If we do, we run the check. If we don't, we wait until
        we need to, or we receive a stop."""

        last_checked = 0
        self.log.debug('Beginning run() loop')
        while True and not self._event.is_set():
            if time.time() - last_checked > self._refresh:
                self.log.debug('[%s] running' % self._command)

                # First, run our service check command and see what the
                # return code is
                c = Command(self._command, self._service)
                ret = c.run(timeout=90)

                if ret == 0:
                    # If the command was successfull...
                    self.log.info('[%s] returned successfully' % self._command)
                    self.update(state=True)
                else:
                    # If the command failed...
                    self.log.warning('[%s] returned a failed exit code [%s]' %
                                     (self._command, ret))
                    self.update(state=False)

                # Now that our service check is done, update our lastrun{}
                # array with the current time, so that we can check how
                # long its been since the last run.
                last_checked = time.time()

            # Sleep for one second just so that we dont run in a crazy loop
            # taking up all kinds of resources.
            self._event.wait(1)

        self.log.debug('Watcher %s is exiting the run() loop.' % self._service)

    def stop(self):
        """Stop the run() loop."""
        self._event.set()
        self.update(False)
        

    def update(self, state):
        # Call ServiceRegistry.set() method with our state, data,
        # path information. The ServiceRegistry module will take care of
        # updating the data, state, etc.
        self.log.debug('Attempting to update service [%s] with '
                       'data [%s], and state [%s].' %
                       (self._service, self._data, state))
        try:
            self._sr.set(self._fullpath, self._data, state)
            self.log.debug('[%s] sucessfully updated path %s with state %s' %
                          (self._service, self._fullpath, state))
            return True
        except Exception, e:
            self.log.warn('[%s] could not update path %s with state %s: %s' %
                         (self._service, self._fullpath, state, e))
            return False


class Command(object):
    """Wrapper to run a command with a timeout for safety."""

    LOGGER = 'WatcherDaemon.Command'

    def __init__(self, cmd, service):
        """Initialize the Command object.

        This object can be created once, and run many times. Each time it
        runs we initiate a small thread to run our process, and if that
        process times out, we kill it."""

        self._cmd = cmd
        self._process = None
        self.log = logging.getLogger('%s.%s' % (self.LOGGER, service))

    def run(self, timeout):
        def target():
            self.log.debug('[%s] started...' % self._cmd)
            # Deliberately do not capture any output. Using PIPEs can
            # cause deadlocks according to the Python documentation here
            # (http://docs.python.org/library/subprocess.html)
            #
            # "Warning This will deadlock when using stdout=PIPE and/or
            # stderr=PIPE and the child process generates enough output to
            # a pipe such that it blocks waiting for the OS pipe buffer to
            # accept more # data. Use communicate() to avoid that."
            #
            # We only care about the exit code of the command anyways...
            try:
                self._process = subprocess.Popen(
                    self._cmd.split(' '),
                    shell=False,
                    stdout=open('/dev/null', 'w'),
                    stderr=None,
                    stdin=None)
                self._process.communicate()
            except OSError, e:
                self.log.warn('Failed to run: %s' % e)
                return 1
            self.log.debug('[%s] finished... returning %s' %
                          (self._cmd, self._process.returncode))

        thread = threading.Thread(target=target)
        thread.start()

        thread.join(timeout)
        if thread.is_alive():
            self.log.debug('[%s] taking too long to respond, terminating.' %
                           self._cmd)
            try:
                self._process.terminate()
            except:
                pass
            thread.join()

        # If the subprocess.Popen() fails for any reason, it returns 1... but
        # because its in a thread, we never actually see that error code.
        if self._process:
            return self._process.returncode
        else:
            return 1


def main():
    # Get our logger
    logger = logging.getLogger()
    pid = os.getpid()
    format = 'zk_watcher[' + str(pid) + ',%(name)s' \
             ',%(funcName)s]: (%(levelname)s) %(message)s'
    formatter = logging.Formatter(format)

    if options.verbose:
        logger.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.INFO)

    if options.syslog:
        handler = logging.handlers.SysLogHandler('/dev/log', 'syslog')
    else:
        handler = logging.StreamHandler()

    handler.setFormatter(formatter)
    logger.addHandler(handler)

    # Define our WatcherDaemon object..
    watcher = WatcherDaemon(
        config_file=options.config,
        server=options.server,
        verbose=options.verbose)

    while True:
        try:
            time.sleep(1)
        except KeyboardInterrupt:
            logger.info('Exiting')
            break

if __name__ == '__main__':
    main()
