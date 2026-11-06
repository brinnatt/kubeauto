#!/usr/bin/env python3
"""In-cluster network-policy assertion: prove DNS, then TCP and verified TLS."""
import os
import socket
import ssl
import time
from urllib.parse import urlsplit


def probe(url, ca, expected):
    endpoint = urlsplit(url)
    if endpoint.scheme != 'https' or endpoint.port != 9200 or expected not in ('allowed', 'denied'):
        raise ValueError('invalid network fixture input')
    addresses = socket.getaddrinfo(endpoint.hostname, endpoint.port, type=socket.SOCK_STREAM)
    if not addresses:
        raise ValueError('network probe cannot resolve the product Service')
    # DNS failures and refused sockets must not be misreported as a policy drop.
    deadline, drops = time.monotonic() + 45, 0
    while time.monotonic() < deadline:
        try:
            connection = socket.create_connection((endpoint.hostname, endpoint.port), timeout=5)
        except TimeoutError:
            drops += 1
            if expected == 'denied' and drops >= 2: return
            continue
        with connection:
            drops = 0
            if expected == 'denied':
                # Namespace label changes propagate asynchronously to the CNI.
                # A temporary open path is observed, never accepted as denial.
                time.sleep(1)
                continue
            context = ssl.create_default_context(cafile=ca)
            with context.wrap_socket(connection, server_hostname=endpoint.hostname) as secured:
                secured.sendall(('GET / HTTP/1.1\r\nHost: ' + endpoint.hostname + '\r\nConnection: close\r\n\r\n').encode())
                first = secured.recv(4096).split(b'\r\n', 1)[0]
                if not first.startswith(b'HTTP/1.1 401 '):
                    raise ValueError('allowed path did not reach the secured Elasticsearch HTTP endpoint')
                return
    raise ValueError('network policy did not converge to its declared ' + expected + ' result')


if __name__ == '__main__':
    try:
        expected = os.environ['ELASTICSEARCH_NETWORK_EXPECTED']
        probe(os.environ['ELASTICSEARCH_URL'], os.environ['ELASTICSEARCH_CA'], expected)
        print('ELASTICSEARCH_NETWORK_PROBE_PASS expected=' + expected, flush=True)
    except Exception as error:
        print('ELASTICSEARCH_NETWORK_PROBE_FAIL reason=' + type(error).__name__, flush=True)
        raise SystemExit(1)
