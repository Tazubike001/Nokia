#!/usr/bin/env python3

import datetime
import logging
import os
import re
import signal
import sys
import time

import vrnetlab

STARTUP_CONFIG_FILE = "/config/startup-config.cfg"


def handle_SIGCHLD(signal, frame):
    os.waitpid(-1, os.WNOHANG)


def handle_SIGTERM(signal, frame):
    sys.exit(0)


signal.signal(signal.SIGINT, handle_SIGTERM)
signal.signal(signal.SIGTERM, handle_SIGTERM)
signal.signal(signal.SIGCHLD, handle_SIGCHLD)

TRACE_LEVEL_NUM = 9
logging.addLevelName(TRACE_LEVEL_NUM, "TRACE")


def trace(self, message, *args, **kws):
    # Yes, logger takes its '*args' as 'args'.
    if self.isEnabledFor(TRACE_LEVEL_NUM):
        self._log(TRACE_LEVEL_NUM, message, args, **kws)


logging.Logger.trace = trace

class XRV_vm(vrnetlab.VM):
    def __init__(self, hostname, username, password, conn_mode, vcpu, ram):
        for e in os.listdir("/"):
            if re.search(".qcow2", e):
                disk_image = "/" + e
        super(XRV_vm, self).__init__(
            username, password, disk_image=disk_image, ram=ram, smp=f"cores={vcpu},threads=1,sockets=1"
        )
        self.hostname = hostname
        self.conn_mode = conn_mode
        self.num_nics = 128
        
        self.xr_ready = False
        self.credentials = []
        
    def bootstrap_spin(self):
        """"""

        if self.spins > 300:
            # too many spins with no result ->  give up
            self.stop()
            self.start()
            return

        (ridx, match, res) = self.expect(
            [
                b"Press RETURN to get started",
                b"SYSTEM CONFIGURATION COMPLETE",
                b"Enter root-system username",
                b"Username:",
                b"#",
            ],
            1,
        )
        if match:  # got a match!
            if ridx == 0:  # press return to get started, so we press return!
                self.logger.info("got 'press return to get started...'")
                self.wait_write("", wait=None)
            if ridx == 1:  # system configuration complete
                self.logger.info(
                    "IOS XR system configuration is complete, should be able to proceed with bootstrap configuration"
                )
                self.wait_write("", wait=None)
                self.xr_ready = True
            if ridx == 2:  # initial user config
                self.logger.info("Creating initial user")
                time.sleep(15)
                self.wait_write(self.username, wait=None)
                self.wait_write(self.password, wait="Enter secret:")
                self.wait_write(self.password, wait="Enter secret again:")
                self.credentials.insert(0, [self.username, self.password])
            if ridx == 3:  # matched login prompt, so should login
                self.logger.info("matched login prompt")
                try:
                    username, password = self.credentials.pop(0)
                except IndexError as exc:
                    self.logger.error("no more credentials to try")
                    return
                self.logger.info(
                    "trying to log in with %s / %s" % (username, password)
                )
                self.wait_write(username, wait=None)
                self.wait_write(password, wait="Password:")
            if self.xr_ready == True and ridx == 4:
                # run main config!
                self.bootstrap_config()
                self.startup_config()
                # close telnet connection
                self.tn.close()
                # startup time?
                startup_time = datetime.datetime.now() - self.start_time
                self.logger.info("Startup complete in: %s" % startup_time)
                # mark as running
                self.running = True
                return

        # no match, if we saw some output from the router it's probably
        # booting, so let's give it some more time
        if res != "":
            self.print(res)
            # reset spins if we saw some output
            self.spins = 0

        self.spins += 1

        return

    def bootstrap_config(self):
        """Do the actual bootstrap config"""
        self.logger.info("applying bootstrap configuration")
        self.wait_write("", None)

        self.wait_write("terminal length 0")

        self.wait_write("crypto key generate rsa")
        
        self.wait_write("2048", wait=None)

        # make sure we get our prompt back
        self.wait_write("")

        self.wait_write("configure")
        self.wait_write("hostname {}".format(self.hostname))
        
        # configure management vrf
        self.wait_write("vrf clab-mgmt")
        self.wait_write("description Containerlab management VRF (DO NOT DELETE)")
        self.wait_write("address-family ipv4 unicast")
        self.wait_write("exit")
        self.wait_write("exit")
        
        # add static route for management
        self.wait_write("router static")
        self.wait_write("vrf clab-mgmt")
        self.wait_write("address-family ipv4 unicast")
        self.wait_write("0.0.0.0/0 10.0.0.2")
        self.wait_write("exit")
        self.wait_write("exit")
        self.wait_write("exit")
      
        # configure ssh & netconf w/ vrf
        self.wait_write("ssh server v2")
        self.wait_write("ssh server vrf clab-mgmt")
        self.wait_write("ssh server netconf port 830")  # for 5.1.1
        self.wait_write("ssh server netconf vrf clab-mgmt")  # for 5.3.3
        self.wait_write("netconf agent ssh")  # for 5.1.1
        self.wait_write("netconf-yang agent ssh")  # for 5.3.3
        
        # configure xml agent
        self.wait_write("xml agent tty")
        
        # configure mgmt interface
        self.wait_write("interface MgmtEth 0/0/CPU0/0")
        self.wait_write("vrf clab-mgmt")
        self.wait_write("no shutdown")
        self.wait_write("ipv4 address 10.0.0.15/24")
        self.wait_write("exit")
        self.wait_write("commit")
        self.wait_write("exit")

    def startup_config(self):
        """Load additional config provided by user."""

        if not os.path.exists(STARTUP_CONFIG_FILE):
            self.logger.warning(f"Startup config file {STARTUP_CONFIG_FILE} is not found")
            return

        self.logger.info(f"Startup config file {STARTUP_CONFIG_FILE} exists")
        with open(STARTUP_CONFIG_FILE) as file:
            config_lines = file.readlines()
            config_lines = [line.rstrip() for line in config_lines]
            self.logger.info(f"Parsed startup config file {STARTUP_CONFIG_FILE}")

        self.logger.info(f"Writing lines from {STARTUP_CONFIG_FILE}")

        self.wait_write("configure")
        # Apply lines from file
        for line in config_lines:
            self.wait_write(line)
        # Commit and GTFO
        self.wait_write("commit")
        self.wait_write("exit")

class XRV(vrnetlab.VR):
    def __init__(self, hostname, username, password, conn_mode, vcpu, ram):
        super(XRV, self).__init__(username, password)
        self.vms = [XRV_vm(hostname, username, password, conn_mode, vcpu, ram)]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="")
    parser.add_argument("--hostname", default="vr-xrv", help="Router hostname")
    parser.add_argument(
        "--trace", action="store_true", help="enable trace level logging"
    )
    parser.add_argument("--username", default="clab", help="Username")
    parser.add_argument("--password", default="clab@123", help="Password")
    parser.add_argument(
        "--connection-mode",
        default="vrxcon",
        help="Connection mode to use in the datapath",
    )
    parser.add_argument(
        "--vcpu", type=int, default=2, help="Number of cpu cores to use"
    )
    parser.add_argument(
        "--ram", type=int, default=4096, help="Amonut of RAM to allocate in MB"
    )
    args = parser.parse_args()

    LOG_FORMAT = "%(asctime)s: %(module)-10s %(levelname)-8s %(message)s"
    logging.basicConfig(format=LOG_FORMAT)
    logger = logging.getLogger()

    logger.setLevel(logging.DEBUG)
    if args.trace:
        logger.setLevel(1)

    vrnetlab.boot_delay()

    vr = XRV(args.hostname, args.username, args.password, args.connection_mode, args.vcpu, args.ram)
    vr.start()