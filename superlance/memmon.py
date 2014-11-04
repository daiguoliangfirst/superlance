#!/usr/bin/env python -u
##############################################################################
#
# Copyright (c) 2007 Agendaless Consulting and Contributors.
# All Rights Reserved.
#
# This software is subject to the provisions of the BSD-like license at
# http://www.repoze.org/LICENSE.txt.  A copy of the license should accompany
# this distribution.  THIS SOFTWARE IS PROVIDED "AS IS" AND ANY AND ALL
# EXPRESS OR IMPLIED WARRANTIES ARE DISCLAIMED, INCLUDING, BUT NOT LIMITED TO,
# THE IMPLIED WARRANTIES OF TITLE, MERCHANTABILITY, AGAINST INFRINGEMENT, AND
# FITNESS FOR A PARTICULAR PURPOSE
#
##############################################################################

# A event listener meant to be subscribed to TICK_60 (or TICK_5)
# events, which restarts any processes that are children of
# supervisord that consume "too much" memory.  Performs horrendous
# screenscrapes of ps output.  Works on Linux and OS X (Tiger/Leopard)
# as far as I know.

# A supervisor config snippet that tells supervisor to use this script
# as a listener is below.
#
# [eventlistener:memmon]
# command=python memmon.py [options]
# events=TICK_60

doc = """\
memmon.py [-c] [-p processname=byte_size] [-g groupname=byte_size]
          [-a byte_size] 

Options:

-c -- Check against cumulative RSS. When calculating a process' RSS, also
      consider its child processes. With this option `memmon` will sum up
      the RSS of the process to be monitored and all its children.

-p -- specify a process_name=byte_size pair.  Restart the supervisor
      process named 'process_name' when it uses more than byte_size
      RSS.  If this process is in a group, it can be specified using
      the 'process_name:group_name' syntax.

-g -- specify a group_name=byte_size pair.  Restart any process in this group
      when it uses more than byte_size RSS.

-a -- specify a global byte_size.  Restart any child of the supervisord
      under which this runs if it uses more than byte_size RSS.


The -p and -g options may be specified more than once, allowing for
specification of multiple groups and processes.

Any byte_size can be specified as a plain integer (10000) or a
suffix-multiplied integer (e.g. 1GB).  Valid suffixes are 'KB', 'MB'
and 'GB'.

A sample invocation:

memmon.py -p program1=200MB -p theprog:thegroup=100MB -g thegroup=100MB -a 1GB 
"""

import os
import sys
import time
from collections import namedtuple

try:
    from sys import maxsize as maxint
except ImportError:
    from sys import maxint
try:
    import xmlrpc.client as xmlrpclib
except ImportError:
    import xmlrpclib
#delete because i get code up
#from superlance.compat import maxint
#from superlance.compat import xmlrpclib

from supervisor import childutils
from supervisor.datatypes import byte_size, SuffixMultiplier

def usage():
    print(doc)
    sys.exit(255)

def shell(cmd):
    with os.popen(cmd) as f:
        return f.read()

class Memmon:
    def __init__(self, cumulative, programs, groups, any,  name, rpc=None):
        self.cumulative = cumulative
        self.programs = programs
        self.groups = groups
        self.any = any
        self.memmonName = name
        self.rpc = rpc
        self.stdin = sys.stdin
        self.stdout = sys.stdout
        self.stderr = sys.stderr
        self.pscommand = 'ps -orss= -p %s'
        self.pstreecommand = 'ps ax -o "pid= ppid= rss="'

    def runforever(self, test=False):
        while 1:
            # we explicitly use self.stdin, self.stdout, and self.stderr
            # instead of sys.* so we can unit test this code
            headers, payload = childutils.listener.wait(self.stdin, self.stdout)

            if not headers['eventname'].startswith('TICK'):
                # do nothing with non-TICK events
                childutils.listener.ok(self.stdout)
                if test:
                    break
                continue

            status = []
            if self.programs:
                keys = sorted(self.programs.keys())
                status.append(
                    'Checking programs %s' % ', '.join(
                    [ '%s=%s' % (k, self.programs[k]) for k in keys ])
                    )

            if self.groups:
                keys = sorted(self.groups.keys())
                status.append(
                    'Checking groups %s' % ', '.join(
                    [ '%s=%s' % (k, self.groups[k]) for k in keys ])
                    )
            if self.any is not None:
                status.append('Checking any=%s' % self.any)

            self.stderr.write('\n'.join(status) + '\n')

            infos = self.rpc.supervisor.getAllProcessInfo()

            for info in infos:
                pid = info['pid']
                name = info['name']
                group = info['group']
                pname = '%s:%s' % (group, name)

                if not pid:
                    # ps throws an error in this case (for processes
                    # in standby mode, non-auto-started).
                    continue

                rss = self.calc_rss(pid)
                if rss is None:
                    # no such pid (deal with race conditions) or
                    # rss couldn't be calculated for other reasons
                    continue

                for n in name, pname:
                    if n in self.programs:
                        self.stderr.write('RSS of %s is %s\n' % (pname, rss))
                        if  rss > self.programs[name]:

##need to modify
                            self.restart(pname, rss)
                            continue

                if group in self.groups:
                    self.stderr.write('RSS of %s is %s\n' % (pname, rss))
                    if rss > self.groups[group]:
##need to modify
                        self.restart(pname, rss)
                        continue

                if self.any is not None:
                    self.stderr.write('RSS of %s is %s\n' % (pname, rss))
                    if rss > self.any:
##need to modify
                        self.restart(pname, rss)
                        continue

            self.stderr.flush()
            childutils.listener.ok(self.stdout)
            if test:
                break


    def calc_rss(self, pid):

        ProcInfo = namedtuple('ProcInfo', ['pid', 'ppid', 'rss'])

        def find_children(parent_pid, procs):
            children = []
            for proc in procs:
                pid, ppid, rss = proc
                if ppid == parent_pid:
                    children.append(proc)
                    children.extend(find_children(pid, procs))
            return children

        def cum_rss(pid, procs):
            parent_proc = [p for p in procs if p.pid == pid][0]
            children = find_children(pid, procs)
            tree = [parent_proc] + children
            total_rss = sum(map(int, [p.rss for p in tree]))
            return total_rss

        def get_all_process_infos(data):
            data = data.strip()
            procs = []
            for line in data.splitlines():
                pid, ppid, rss = map(int, line.split())
                procs.append(ProcInfo(pid=pid, ppid=ppid, rss=rss))
            return procs

        if self.cumulative:
            data = shell(self.pstreecommand)
            procs = get_all_process_infos(data)

            try:
                rss = cum_rss(pid, procs)
            except (ValueError, IndexError):
                # Could not determine cumulative RSS
                return None

        else:
            data = shell(self.pscommand % pid)
            if not data:
                # no such pid (deal with race conditions)
                return None

            try:
                rss = data.lstrip().rstrip()
                rss = int(rss)
            except ValueError:
                # line doesn't contain any data, or rss cant be intified
                return None

        rss = rss * 1024  # rss is in KB
        return rss


def parse_namesize(option, value):
    try:
        name, size = value.split('=')
    except ValueError:
        print('Unparseable value %r for %r' % (value, option))
        usage()
    size = parse_size(option, size)
    return name, size

def parse_size(option, value):
    try:
        size = byte_size(value)
    except:
        print('Unparseable byte_size in %r for %r' % (value, option))
        usage()

    return size

seconds_size = SuffixMultiplier({'s': 1,
                                 'm': 60,
                                 'h': 60 * 60,
                                 'd': 60 * 60 * 24
                                 })

def parse_seconds(option, value):
    try:
        seconds = seconds_size(value)
    except:
        print('Unparseable value for time in %r for %s' % (value, option))
        usage()
    return seconds

def memmon_from_args(arguments):
    import getopt
    short_args = "hcp:g:a:"
    long_args = [
        "help",
        "cumulative",
        "program=",
        "group=",
        "any="
        ]

    if not arguments:
        return None
    try:
        opts, args = getopt.getopt(arguments, short_args, long_args)
    except:
        return None

    cumulative = False
    programs = {}
    groups = {}
    any = None

    for option, value in opts:

        if option in ('-h', '--help'):
            return None

        if option in ('-c', '--cumulative'):
            cumulative = True

        if option in ('-p', '--program'):
            name, size = parse_namesize(option, value)
            programs[name] = size

        if option in ('-g', '--group'):
            name, size = parse_namesize(option, value)
            groups[name] = size

        if option in ('-a', '--any'):
            size = parse_size(option, value)
            any = size


    memmon = Memmon(cumulative=cumulative,
                    programs=programs,
                    groups=groups,
                    any=any
                    )
    return memmon

def main():
    memmon = memmon_from_args(sys.argv[1:])
    if memmon is None:
        # something went wrong or -h has been given
        usage()
    memmon.rpc = childutils.getRPCInterface(os.environ)
    memmon.runforever()

if __name__ == '__main__':
    main()
