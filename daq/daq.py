#!/usr/bin/env python

"""Device Automated Qualification testing framework"""

import logging
import os
import re
import subprocess
import time

from mininet import log as minilog
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch, Host
from mininet.cli import CLI
from mininet.util import pmonitor

from tests.faucet_mininet_test_host import MakeFaucetDockerHost
from tests.faucet_mininet_test_topo import FaucetHostCleanup
from tests import faucet_mininet_test_util

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)


class DAQHost(FaucetHostCleanup, Host):
    """Base Mininet Host class, for Mininet-based tests."""

    pass


class DAQRunner():
    DHCP_PATTERN = '> ([0-9.]+).68: BOOTP/DHCP, Reply'
    net = None
    switch = None

    def addHost(self, name, cls=DAQHost, ip=None, env_vars=[]):
        tmpdir = 'inst/'
        params = { 'ip': ip } if ip else {}
        params['tmpdir'] = tmpdir
        params['env_vars'] = env_vars
        host = self.net.addHost(name, cls, **params)
        host.switch_link = self.net.addLink(self.switch, host, fast=False)
        if self.net.built:
            host.configDefault()
            self.switch.attach(host.switch_link.intf1.name)
        return host

    def stopHost(self, host):
        logging.debug("Stopping host " + host.name)
        host.terminate()

    def pingTest(self, a, b):
        logging.debug("Ping test %s->%s" % (a.name, b.name))
        failure="ping FAILED"
        assert b.IP() != "0.0.0.0", "IP address not assigned, can't ping"
        output = a.cmd('ping -c1', b.IP(), '> /dev/null 2>&1 || echo ', failure).strip()
        if output:
            print output
        return output.strip() != failure


    def tcpdump_helper(self, tcpdump_host, tcpdump_filter, funcs=None,
                       vflags='-v', timeout=10, packets=2, root_intf=False,
                       intf=None):
        intf_name = (intf if intf else tcpdump_host.intf()).name
        if root_intf:
            intf_name = intf_name.split('.')[0]
        tcpdump_cmd = faucet_mininet_test_util.timeout_soft_cmd(
            'tcpdump -i %s -e -n -U %s -c %u %s' % (
                intf_name, vflags, packets, tcpdump_filter),
            timeout)
        tcpdump_out = tcpdump_host.popen(
            tcpdump_cmd,
            stdin=faucet_mininet_test_util.DEVNULL,
            stderr=subprocess.STDOUT,
            close_fds=True)
        popens = {tcpdump_host: tcpdump_out}
        tcpdump_started = False
        tcpdump_lines = []
        for host, line in pmonitor(popens):
            if host == tcpdump_host:
                tcpdump_lines += [line]
                if not tcpdump_started and re.search('listening on %s' % intf_name, line):
                    tcpdump_started = True
                    tcpdump_lines = []
                    # when we see tcpdump start, then call provided functions.
                    if funcs is not None:
                        for func in funcs:
                            func()
        assert tcpdump_started, 'tcpdump did not start: %s' % tcpdump_lines
        return tcpdump_lines


    def createNetwork(self):
        logging.debug("Creating miniet...")
        self.net = Mininet()

        logging.debug("Adding switch...")
        self.switch = self.net.addSwitch('s1', cls=OVSSwitch)

        logging.debug("Starting faucet container...")
        self.switch.cmd('cmd/faucet')

        targetIp = "127.0.0.1"
        logging.debug("Adding controller at %s" % targetIp)
        c1 = self.net.addController( 'c1', controller=RemoteController, ip=targetIp, port=6633 )

        logging.debug("Adding hosts...")
        h1 = self.addHost('h1', cls=MakeFaucetDockerHost('daq/networking'))
        h2 = self.addHost('h2', cls=MakeFaucetDockerHost('daq/fauxdevice'), ip="0.0.0.0")
        h3 = self.addHost('h3')

        logging.debug("Starting mininet...")
        self.net.start()

        logging.debug("Activating networking...")
        h1.activate()

        logging.debug("Waiting for system to settle...")
        time.sleep(3)

        self.pingTest(h1, h3)
        self.pingTest(h3, h1)

        assert not self.pingTest(h2, h1), "Unexpected success??!?!"
        print "(Expected failure)"

        h2.activate()

        logging.debug("Waiting for dhcp...")
        filter="src port 67"
        dhcp_lines = self.tcpdump_helper(self.switch, filter, intf=h1.switch_link.intf1, vflags='', packets=1)
        ip_addr = re.search(self.DHCP_PATTERN, dhcp_lines[0]).group(1)
        print "h2 ip address is " + ip_addr
        h2.setIP(ip_addr)

        self.pingTest(h2, h1)
        self.pingTest(h1, h2)

        logging.debug("Creating/activating proper test_ping...")
        env_vars = [ "TARGET_HOST=" + h1.IP() ]
        h4 = self.addHost('h4', cls=MakeFaucetDockerHost('daq/test_ping'), env_vars = env_vars)
        h4.activate()
        assert h4.check_result() == 0, "test_ping to %s" % h1.IP()

        logging.debug("Creating/activating bogus test_ping...")
        env_vars = [ "TARGET_HOST=1.2.3.4" ]
        h5 = self.addHost('h5', cls=MakeFaucetDockerHost('daq/test_ping'), env_vars = env_vars)
        h5.activate()
        assert h5.check_result() != 0, "test_ping to bogus address"

        CLI(self.net)

        logging.debug("Stopping faucet...")
        self.switch.cmd('docker kill daq-faucet')
        logging.debug("Stopping mininet...")
        self.net.stop()


if __name__ == '__main__':
    minilog.setLogLevel('info')
    if os.getuid() == 0:
        DAQRunner().createNetwork()
    else:
        logger.debug("You are NOT root")
