#!/usr/bin/env python3
"""Send files from this PC to an iPhone over AirDrop, through awdl_netd.

Why this exists: netd only accepted *inbound* connections (phone -> PC). The
outbound proxy (opt-in, `AWDL_NETD_OUTBOUND`) lets a local process name the far
end and then speaks the normal AirDrop protocol over it. The protocol itself is
reused from upstream `airdrop/sender.py` (MIT); this file only replaces its two
transport assumptions:

  * discovery: the interface browser is replaced by queries through netd's mDNS
    proxy (127.0.0.1:15353), which already carries AWDL multicast;
  * connection: the Windows link-local 4-tuple connect is replaced by a local
    connection to netd's outbound proxy plus a `CONNECT <ipv6> <port>` line.

This is an experimental sender. Use "Everyone for 10 Minutes" on the iPhone.
An iPhone may still reject the self-signed certificate in that mode.
"""
from __future__ import annotations

import argparse
import http.client
import ipaddress
import logging
import socket
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
AIRDROP = REPO / 'upstream' / 'winawdl' / 'airdrop'
sys.path.insert(0, str(AIRDROP))

import identity  # noqa: E402  (upstream modules live on the path above)
import mdns  # noqa: E402
import sender  # noqa: E402

SERVICE_TYPE = '_airdrop._tcp.local'
DEFAULT_PROXY_PORT = 8772
DEFAULT_MDNS_PORT = 15353


class ProxyBrowser:
    """Receiver discovery through netd's mDNS proxy instead of an interface."""

    def __init__(self, port=DEFAULT_MDNS_PORT, service_type=SERVICE_TYPE):
        self.port = port
        self.instances = mdns.Instances(service_type)
        self.heard = 0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(('127.0.0.1', 0))
        self.sock.settimeout(0.3)
        self._next_ask = 0.0

    def poll(self):
        now = time.monotonic()
        if now >= self._next_ask:
            self.sock.sendto(mdns.query(self.instances.questions()), ('127.0.0.1', self.port))
            self._next_ask = now + 1.0
        deadline = now + 0.5
        while time.monotonic() < deadline:
            try:
                data, _ = self.sock.recvfrom(9000)
            except socket.timeout:
                break
            self.instances.feed(data, None)
            self.heard += 1
        return {address: (address, port)
                for address, (_, port) in self.instances.devices(within=30).items()
                if address and 1 <= port <= 65535}

    def close(self):
        self.sock.close()


class ProxyConnection(http.client.HTTPConnection):
    """HTTPS to a link-local receiver, tunnelled through netd's outbound proxy."""

    proxy_port = DEFAULT_PROXY_PORT

    def __init__(self, address, port, scope, context, timeout):
        super().__init__(address, port, timeout=timeout)
        self.context = context

    def connect(self):
        sock = socket.create_connection(('127.0.0.1', self.proxy_port), timeout=self.timeout)
        try:
            sock.settimeout(self.timeout)
            sock.sendall(f'CONNECT {self.host} {self.port}\n'.encode('ascii'))
            self.sock = self.context.wrap_socket(sock)
        except OSError:
            sock.close()
            raise


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('files', nargs='*', help='files to send')
    parser.add_argument('--to', help='part of the receiver name, when several answer')
    parser.add_argument('--address',
                        help='connect directly to this IPv6 address (requires --port)')
    parser.add_argument('--port', type=int,
                        help='receiver port from its mDNS SRV record (requires --address)')
    parser.add_argument('--search', type=int, default=20,
                        help='seconds to look for receivers (default 20)')
    parser.add_argument('--name', default=socket.gethostname().split('.')[0],
                        help='name the phone shows in its prompt')
    parser.add_argument('--keys', default=str(REPO / 'dist' / 'DropBridge' / 'data' / 'keys'),
                        help='directory with certificate.self-signed.pem and the key')
    parser.add_argument('--proxy-port', type=int, default=DEFAULT_PROXY_PORT)
    parser.add_argument('--mdns-port', type=int, default=DEFAULT_MDNS_PORT)
    parser.add_argument('--trace', action='store_true')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--list', action='store_true',
                      help='list mDNS endpoints without connecting to them')
    mode.add_argument('--probe', action='store_true',
                      help='test /Discover and report TLS errors; no file is sent')
    settings = parser.parse_args(argv)
    if settings.address and not (settings.port and 1 <= settings.port <= 65535):
        parser.error('--address requires --port in 1..65535 (use the receiver SRV port)')
    if settings.address:
        try:
            settings.address = str(ipaddress.IPv6Address(settings.address.split('%')[0]))
        except ValueError:
            parser.error('--address must be an IPv6 address')
    if settings.port is not None and not settings.address:
        parser.error('--port requires --address')
    if settings.list and settings.address:
        parser.error('--list discovers endpoints; use --probe to test a known endpoint')
    if settings.list or settings.probe:
        if settings.files or settings.to:
            parser.error('--list and --probe do not accept files or --to')
    elif not settings.files:
        parser.error('supply files to send, or use --list or --probe')
    return settings


def discover_endpoints(port, seconds):
    browser = ProxyBrowser(port)
    endpoints = {}
    try:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            endpoints.update(browser.poll())
    finally:
        browser.close()
    logging.info('%d mDNS packets came back through the proxy', browser.heard)
    return endpoints


def probe(address, port, context):
    try:
        info = sender.receiver_identity(address, port, None, context)
    except sender.Unwilling as error:
        logging.warning('[%s]:%d rejected the TLS certificate: %s', address, port, error)
    except sender.Unreachable as error:
        logging.warning('[%s]:%d /Discover failed: %s', address, port, error)
    else:
        if info:
            logging.info('[%s]:%d /Discover answered: %s', address, port, info[0])
            return True
        logging.warning('[%s]:%d /Discover returned no receiver name', address, port)
    return False


def main(argv=None):
    settings = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if settings.trace else logging.INFO,
                        stream=sys.stdout,
                        format='%(asctime)s %(levelname)s %(message)s')
    ProxyConnection.proxy_port = settings.proxy_port
    sender.Connection = ProxyConnection      # every sender helper now tunnels
    if settings.list or (settings.probe and not settings.address):
        endpoints = discover_endpoints(settings.mdns_port, settings.search)
        if not endpoints:
            logging.warning('no AirDrop mDNS endpoint found. Unlock the iPhone and '
                            'refresh "Everyone for 10 Minutes".')
            return 2
        for address, port in sorted(endpoints.values()):
            logging.info('mDNS endpoint: [%s]:%d', address, port)
        if settings.list:
            return 0

    try:
        context = sender.client_context(Path(settings.keys))
    except identity.IdentityError as error:
        raise SystemExit(str(error)) from None
    if settings.probe:
        if settings.address:
            return 0 if probe(settings.address, settings.port, context) else 1
        results = [probe(address, port, context) for address, port in sorted(endpoints.values())]
        return 0 if any(results) else 1

    files = sender.outgoing(settings.files)

    if settings.address:
        # Direct path: discovery is skipped because the caller already knows the
        # link-local address (for example from netd's mDNS source log).
        address = settings.address
        logging.info('sending straight to [%s]:%d; accept on the phone', address, settings.port)
        try:
            ok = sender.send(address, settings.port, None, context, settings.name, files)
        except TimeoutError:
            logging.warning('%s did not answer in time', address)
            return 1
        except (OSError, http.client.HTTPException) as error:
            logging.warning('the connection to %s broke: %s', address,
                            error or type(error).__name__)
            return 1
        if ok:
            logging.info('sent %d file(s) to %s', len(files), address)
            return 0
        logging.warning('the phone did not take the files')
        return 1

    browser = ProxyBrowser(settings.mdns_port)
    try:
        named = sender.find_receivers(browser, None, context, settings.to, settings.search)
        logging.info('%d mDNS packets came back through the proxy', browser.heard)
    finally:
        browser.close()
    if not named:
        logging.warning('no receiver returned its name. Unlock the iPhone and refresh '
                        '"Everyone for 10 Minutes"; use --list and --probe to diagnose.')
        return 2
    for name, (address, port) in sorted(named.items()):
        logging.info('candidate: %s at %s port %d', name, address, port)

    name, (address, port) = sender.choose(named, settings.to)
    logging.info('asking %s at %s; accept on the phone', name, address)
    try:
        ok = sender.send(address, port, None, context, settings.name, files)
    except TimeoutError:
        logging.warning('%s did not answer in time', name)
        return 1
    except (OSError, http.client.HTTPException) as error:
        logging.warning('the connection to %s broke: %s', name, error or type(error).__name__)
        return 1
    if ok:
        logging.info('sent %d file(s) to %s', len(files), name)
        return 0
    logging.warning('the phone did not take the files')
    return 1


if __name__ == '__main__':
    sys.exit(main())
