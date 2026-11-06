#!/usr/bin/env python3
"""Business assertions executed inside the pinned official Python client image.

Credentials are mounted files. Output contains counts, hashes and bounded
measurements; never API keys, authentication headers or response bodies.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import parse_qsl, urlsplit


def require(ok, message):
    if not ok:
        raise ValueError(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def connect(*, api_key=None, basic_auth=None, url=None, ca=None):
    from elasticsearch import Elasticsearch
    require(re.fullmatch(r'https://[a-z0-9.-]+:9200', url or os.environ['ELASTICSEARCH_URL']), 'HTTPS client endpoint required')
    options = dict(ca_certs=ca or os.environ['ELASTICSEARCH_CA'], request_timeout=20,
                   max_retries=0, retry_on_timeout=False)
    if os.environ.get('ELASTICSEARCH_ASSERT_HOSTNAME'):
        options['ssl_assert_hostname'] = os.environ['ELASTICSEARCH_ASSERT_HOSTNAME']
    if api_key:
        options['api_key'] = api_key
    elif basic_auth is not None:
        options['basic_auth'] = basic_auth
    else:
        username = Path(os.environ['ELASTICSEARCH_USERNAME_FILE']).read_text().strip() if os.environ.get('ELASTICSEARCH_USERNAME_FILE') else os.environ.get('ELASTICSEARCH_USERNAME', 'elastic')
        options['basic_auth'] = (username, Path(os.environ['ELASTICSEARCH_PASSWORD_FILE']).read_text().strip())
    return Elasticsearch(url or os.environ['ELASTICSEARCH_URL'], **options)


def call(client, method, path, body=None, *, expected=(200, 201), ndjson=False):
    from elasticsearch import ApiError
    target = urlsplit(path)
    headers = {'accept': 'application/vnd.elasticsearch+json; compatible-with=9'}
    if body is not None:
        headers['content-type'] = ('application/x-ndjson' if ndjson else 'application/vnd.elasticsearch+json; compatible-with=9')
    try:
        response = client.perform_request(method, target.path, params=dict(parse_qsl(target.query)), body=body, headers=headers)
        status, data = response.meta.status, response.body
    except ApiError as exc:
        status, data = exc.meta.status, exc.body
    require(status in expected, 'unexpected Elasticsearch API status method=' + method + ' path=' + target.path + ' status=' + str(status))
    return data


def write_result(body, minimum=2):
    shards = body.get('_shards', {})
    require(shards.get('failed') == 0 and shards.get('successful', 0) >= minimum, 'write acknowledgement did not cover the expected healthy copies')


def mapping():
    return {'settings': {'number_of_shards': 3, 'number_of_replicas': 1, 'refresh_interval': '-1', 'index.translog.durability': 'request'},
            'mappings': {'dynamic': 'strict', 'properties': {'business_id': {'type': 'keyword'}, 'title': {'type': 'text'}, 'category': {'type': 'keyword'}, 'price': {'type': 'integer'}}}}


def read_documents(client, index, expected):
    result = call(client, 'POST', '/' + index + '/_mget', {'ids': sorted(expected)})
    actual = {row['_id']: row['_source'] for row in result['docs'] if row.get('found')}
    require(len(result['docs']) == len(expected) and actual == expected, 'document IDs or canonical content differ from the confirmation ledger')
    require(call(client, 'GET', '/' + index + '/_count')['count'] == len(expected), 'index contains missing or extra documents')
    return digest(actual)


def bounded_create(client, index, identifier, document, *, attempts=3):
    """Retry only rejected writes; resolve an unknown outcome by its stable ID."""
    from elastic_transport import ConnectionTimeout
    path = '/' + index + '/_create/' + identifier + '?wait_for_active_shards=all'
    for attempt in range(attempts):
        try:
            result = call(client, 'PUT', path, document, expected=(200, 201, 429))
            if result.get('error'):
                require(result['error'].get('type') == 'es_rejected_execution_exception', 'retry refusal is not a rejected execution')
                if attempt + 1 < attempts:
                    time.sleep(.1 * 2**attempt)
                continue
            return result
        except ConnectionTimeout:
            observed = call(client, 'GET', '/' + index + '/_doc/' + identifier, expected=(200, 404))
            if observed.get('found'):
                require(observed['_source'] == document, 'unknown write conflicts with the stable-ID ledger')
                return {'result': 'confirmed-by-readback', 'unknown_outcome': True}
            raise ValueError('unknown write remains unconfirmed; stop rather than replay blindly')
    raise ValueError('bounded write retries exhausted')


def retry_business(client, index, document):
    """Deterministic client-boundary faults, with every accepted write on ES.

    The injected 429 never reaches ES. The response-loss fault executes a real
    create first, then discards its reply. This is a fixture, not a measured
    server overload or network outage.
    """
    from elastic_transport import ConnectionTimeout

    class FaultClient:
        def __init__(self, mode):
            self.mode, self.calls = mode, 0

        def perform_request(self, method, path, **kwargs):
            self.calls += 1
            if self.mode == 'reject' and self.calls <= 2:
                from types import SimpleNamespace
                return SimpleNamespace(meta=SimpleNamespace(status=429), body={'error': {'type': 'es_rejected_execution_exception'}})
            result = client.perform_request(method, path, **kwargs)
            if self.mode == 'response-loss' and self.calls == 1:
                raise ConnectionTimeout('fixture discarded an acknowledged create response')
            return result

    rejected = FaultClient('reject')
    write_result(bounded_create(rejected, index, 'bounded-retry', document))
    require(rejected.calls == 3, '429 retry budget was not observed')
    lost = FaultClient('response-loss')
    result = bounded_create(lost, index, 'unknown-write', document)
    require(result.get('unknown_outcome') and lost.calls == 2, 'unknown outcome was replayed rather than read back')
    for identifier in ('bounded-retry', 'unknown-write'):
        require(call(client, 'GET', '/' + index + '/_doc/' + identifier)['_source'] == document, 'confirmation ledger differs')
        write_result(call(client, 'DELETE', '/' + index + '/_doc/' + identifier + '?wait_for_active_shards=all'))


def business(client, prefix):
    first, second, alias = prefix + '-a', prefix + '-b', prefix + '-live'
    indices = []
    completed = []
    try:
        call(client, 'GET', '/' + first, expected=(404,))
        indices.append(first)
        require(call(client, 'PUT', '/' + first, mapping())['acknowledged'], 'index declaration not acknowledged')
        health = call(client, 'GET', '/_cluster/health/' + first + '?wait_for_status=green&timeout=60s')
        require(not health.get('timed_out') and health['status'] == 'green', 'test index replicas unavailable')
        seed = {'business_id': 'seed', 'title': 'visibility boundary', 'category': 'test', 'price': 0}
        write_result(call(client, 'PUT', '/' + first + '/_doc/seed?wait_for_active_shards=all', seed))
        require(call(client, 'GET', '/' + first + '/_doc/seed')['_source'] == seed, 'realtime read missed the acknowledged document')
        require(call(client, 'POST', '/' + first + '/_search', {'query': {'match_all': {}}, 'track_total_hits': True})['hits']['total']['value'] == 0, 'refresh-disabled index became searchable unexpectedly')
        call(client, 'POST', '/' + first + '/_refresh')
        require(call(client, 'GET', '/' + first + '/_count')['count'] == 1, 'refresh did not expose the acknowledged document')
        settings = call(client, 'GET', '/' + first + '/_settings')[first]['settings']['index']
        require(settings['translog']['durability'] == 'request', 'request translog durability was not effective')
        write_result(call(client, 'DELETE', '/' + first + '/_doc/seed?wait_for_active_shards=all'))
        completed.append('ES-16')
        expected = {str(i): {'business_id': str(i), 'title': 'elasticsearch distributed search', 'category': 'even' if i % 2 == 0 else 'odd', 'price': i} for i in range(100)}
        lines = []
        for key, doc in expected.items():
            lines.extend((json.dumps({'create': {'_index': first, '_id': key}}), json.dumps(doc)))
        result = call(client, 'POST', '/_bulk?wait_for_active_shards=all', '\n'.join(lines) + '\n', ndjson=True)
        require(not result['errors'] and len(result['items']) == 100, 'bulk HTTP success contains failed or missing items')
        for item in result['items']:
            require(item['create']['status'] == 201, 'bulk create did not create the expected stable ID')
            write_result(item['create'])
        call(client, 'POST', '/' + first + '/_refresh')
        original_hash = read_documents(client, first, expected)
        completed.append('ES-15')
        search = call(client, 'POST', '/' + first + '/_search', {'query': {'bool': {'must': [{'match': {'title': 'distributed'}}], 'filter': [{'term': {'category': 'even'}}]}}, 'sort': [{'price': 'asc'}, {'business_id': 'asc'}], 'size': 100, 'track_total_hits': True})
        require(search['hits']['total']['value'] == 50 and [h['_source']['price'] for h in search['hits']['hits']] == list(range(0, 100, 2)), 'full text, filter or sort result differs')
        aggregation = call(client, 'POST', '/' + first + '/_search', {'size': 0, 'aggs': {'categories': {'terms': {'field': 'category'}}, 'sum_price': {'sum': {'field': 'price'}}}})['aggregations']
        require({r['key']: r['doc_count'] for r in aggregation['categories']['buckets']} == {'even': 50, 'odd': 50} and aggregation['sum_price']['value'] == 4950, 'aggregation differs from the deterministic corpus')
        tokens = call(client, 'POST', '/_analyze', {'analyzer': 'standard', 'text': '中华人民共和国'})['tokens']
        require([t['token'] for t in tokens] == list('中华人民共和国'), 'built-in Chinese standard analyzer differs')
        completed.append('ES-17')
        call(client, 'GET', '/' + second, expected=(404,))
        indices.append(second)
        require(call(client, 'PUT', '/' + second, mapping())['acknowledged'], 'target index declaration not acknowledged')
        call(client, 'POST', '/_aliases', {'actions': [{'add': {'index': first, 'alias': alias, 'is_write_index': True}}]})
        task = call(client, 'POST', '/_reindex?wait_for_completion=false', {'source': {'index': first}, 'dest': {'index': second, 'op_type': 'create'}})['task']
        deadline = time.monotonic() + 300
        while True:
            result = call(client, 'GET', '/_tasks/' + task)
            if result.get('completed'):
                require(not result.get('error') and not result['response'].get('failures') and result['response']['created'] == 100, 'reindex failed or did not copy the complete source')
                break
            require(time.monotonic() < deadline, 'reindex completion timed out')
            time.sleep(1)
        call(client, 'POST', '/' + second + '/_refresh')
        require(read_documents(client, second, expected) == original_hash, 'reindex canonical hash differs')
        require(call(client, 'POST', '/_aliases', {'actions': [{'remove': {'index': first, 'alias': alias}}, {'add': {'index': second, 'alias': alias, 'is_write_index': True}}]})['acknowledged'], 'atomic alias switch not acknowledged')
        require(set(call(client, 'GET', '/_alias/' + alias)) == {second}, 'alias still targets the previous index')
        require(call(client, 'GET', '/' + alias + '/_count')['count'] == 100, 'alias business read failed after switch')
        completed.append('ES-18')
        before = call(client, 'GET', '/' + alias + '/_doc/0')
        changed = dict(expected['0'], price=999)
        condition = '?wait_for_active_shards=all&if_seq_no=' + str(before['_seq_no']) + '&if_primary_term=' + str(before['_primary_term'])
        write_result(call(client, 'PUT', '/' + alias + '/_doc/0' + condition, changed))
        error = call(client, 'PUT', '/' + alias + '/_doc/0' + condition, changed, expected=(409,))
        require(error['error']['type'] == 'version_conflict_engine_exception', 'stale conditional update failed for an unrelated reason')
        error = call(client, 'PUT', '/' + alias + '/_create/0', changed, expected=(409,))
        require(error['error']['type'] == 'version_conflict_engine_exception', 'stable ID replay failed for an unrelated reason')
        expected['0'] = changed
        call(client, 'POST', '/' + second + '/_refresh')
        final_hash = read_documents(client, second, expected)
        completed.append('ES-19')
        retry_business(client, second, changed)
        completed.append('ES-20')
        return {'case_ids': completed, 'documents': 100, 'source_hash': original_hash, 'post_occ_hash': final_hash}
    finally:
        primary = sys.exc_info()[1]
        errors = []
        for index in reversed(indices):
            try:
                require(index.startswith(prefix + '-'), 'cleanup escaped the declared index prefix')
                result = call(client, 'DELETE', '/' + index, expected=(200, 404))
                require(result.get('acknowledged') or result.get('error', {}).get('type') == 'index_not_found_exception', 'business index cleanup not acknowledged')
                missing = call(client, 'GET', '/' + index, expected=(404,))
                require(missing['error']['type'] == 'index_not_found_exception', 'index cleanup readback failed for an unrelated reason')
            except Exception as error:
                errors.append(type(error).__name__)
        if errors:
            if primary is None: raise ValueError('business exact-name cleanup incomplete')
            print('ELASTICSEARCH_BUSINESS_CLEANUP_FAIL count=' + str(len(errors)), file=sys.stderr)


def lifecycle(client, prefix):
    """Own every policy/template by exact name, including failure cleanup."""
    pipeline, component, template, policy, stream = (prefix + suffix for suffix in ('-pipe', '-component', '-template', '-ilm', '-stream'))
    owned = []
    def declare(path, body):
        # Refuse an existing identity; register intent before an uncertain PUT.
        call(client, 'GET', path, expected=(404,))
        owned.append(path)
        require(call(client, 'PUT', path, body).get('acknowledged'), 'lifecycle declaration not acknowledged')
    try:
        declare('/_ingest/pipeline/' + pipeline, {'processors': [{'set': {'field': 'category', 'value': 'normalized'}}]})
        simulated = call(client, 'POST', '/_ingest/pipeline/' + pipeline + '/_simulate', {'docs': [{'_source': {'business_id': 'probe'}}]})
        require(simulated['docs'][0]['doc']['_source']['category'] == 'normalized', 'pipeline processor result differs')
        declare('/_component_template/' + component, {'template': {'settings': {'number_of_shards': 3, 'number_of_replicas': 1, 'index.default_pipeline': pipeline},
                'mappings': {'dynamic': 'strict', 'properties': {'@timestamp': {'type': 'date'}, 'business_id': {'type': 'keyword'}, 'category': {'type': 'keyword'}, 'price': {'type': 'integer'}}}}})
        declare('/_ilm/policy/' + policy, {'policy': {'phases': {'hot': {'actions': {'rollover': {'max_docs': 2}}}, 'delete': {'min_age': '7d', 'actions': {'delete': {}}}}}})
        declare('/_index_template/' + template, {'index_patterns': [stream+'*'], 'priority': 501, 'composed_of': [component], 'data_stream': {}, 'template': {'settings': {'index.lifecycle.name': policy}}})
        call(client, 'GET', '/_data_stream/' + stream, expected=(404,))
        owned.append('/_data_stream/' + stream)
        require(call(client, 'PUT', '/_data_stream/' + stream)['acknowledged'], 'data stream creation not acknowledged')
        health = call(client, 'GET', '/_cluster/health/' + stream + '?wait_for_status=green&timeout=60s')
        require(health['status'] == 'green' and not health.get('timed_out'), 'data stream replicas unavailable')
        for i in range(2):
            write_result(call(client, 'PUT', '/' + stream + '/_create/' + str(i) + '?wait_for_active_shards=all', {'@timestamp': '2026-01-01T00:00:00Z', 'business_id': str(i), 'price': i}))
        rejected = call(client, 'PUT', '/' + stream + '/_create/invalid', {'@timestamp': '2026-01-01T00:00:00Z', 'business_id': 'invalid', 'price': 'not-an-integer'}, expected=(400,))
        require(rejected['error']['type'] in ('document_parsing_exception', 'mapper_parsing_exception'), 'mapping rejection had an unrelated cause')
        call(client, 'POST', '/' + stream + '/_refresh')
        require(call(client, 'GET', '/' + stream + '/_count')['count'] == 2, 'mapping rejection changed valid documents')
        result = call(client, 'POST', '/' + stream + '/_search', {'size': 10, 'sort': ['business_id']})
        require([h['_source']['category'] for h in result['hits']['hits']] == ['normalized', 'normalized'], 'default pipeline was not applied to stream writes')
        before = call(client, 'GET', '/_data_stream/' + stream)['data_streams'][0]
        rollover = call(client, 'POST', '/' + stream + '/_rollover', {})
        require(rollover['rolled_over'] and rollover['old_index'] != rollover['new_index'], 'explicit stream rollover did not change the write index')
        after = call(client, 'GET', '/_data_stream/' + stream)['data_streams'][0]
        require(len(after['indices']) == len(before['indices']) + 1, 'rollover backing-index generation differs')
        explanation = call(client, 'GET', '/' + stream + '/_ilm/explain')['indices']
        require(explanation and all(row['managed'] and row['policy'] == policy for row in explanation.values()), 'backing index is not managed by the declared ILM policy')
        require(call(client, 'GET', '/_ilm/policy/' + policy)[policy]['policy']['phases']['delete']['min_age'] == '7d', 'retention boundary differs')
        require(call(client, 'GET', '/' + stream + '/_count')['count'] == 2, 'rollover lost data or retention deleted it early')
        return {'case_ids': ['ES-21', 'ES-22'], 'documents': 2, 'backing_indices': len(after['indices']), 'retention': '7d', 'rollover_trigger': 'explicit API; automatic scheduler not timed'}
    finally:
        primary = sys.exc_info()[1]
        errors = []
        for path in reversed(owned):
            try:
                call(client, 'DELETE', path, expected=(200, 404))
                call(client, 'GET', path, expected=(404,))
            except Exception as error:
                errors.append(type(error).__name__)
        if errors:
            if primary is None: raise ValueError('lifecycle exact-name cleanup incomplete')
            print('ELASTICSEARCH_LIFECYCLE_CLEANUP_FAIL count=' + str(len(errors)), file=sys.stderr)


def certificate_refusal(client):
    import ssl
    from elastic_transport import TlsError
    from urllib3.util.ssl_match_hostname import CertificateError
    try:
        call(client, 'GET', '/')
    except TlsError as error:
        # Transport 9.4.2 clears errors on the zero-retry exit, so str(error)
        # can be only "TLS error". The original certificate cause survives.
        cause, seen = error, set()
        while cause is not None and id(cause) not in seen:
            if isinstance(cause, (ssl.SSLCertVerificationError, CertificateError)):
                return
            seen.add(id(cause))
            cause = cause.__cause__ or cause.__context__
        raise ValueError('negative TLS probe did not fail on certificate validation') from error
    raise ValueError('untrusted CA or wrong-host certificate was accepted')


def security(client, prefix):
    import socket
    import certifi
    username = Path('/limited/username').read_text().strip()
    password = Path('/limited/password').read_text().strip()
    allowed = prefix.replace('es-gate-', 'es-gate-allowed-', 1)
    denied = prefix.replace('es-gate-', 'es-gate-denied-', 1)
    created, keys, clients = [], [], []
    limited = connect(basic_auth=(username, password)); clients.append(limited)
    def api_key():
        result = call(limited, 'POST', '/_security/api_key', {'name': prefix, 'expiration': '1h',
                      'role_descriptors': {'read-owned': {'cluster': [], 'indices': [{'names': [allowed], 'privileges': ['read']}]}}})
        keys.append(result['id'])
        key_client = connect(api_key=result['encoded']); clients.append(key_client)
        return result['id'], key_client
    try:
        for index in (allowed, denied):
            call(client, 'GET', '/' + index, expected=(404,))
            created.append(index)
            call(client, 'PUT', '/' + index, mapping())
            healthy = call(client, 'GET', '/_cluster/health/' + index + '?wait_for_status=green&timeout=60s')
            require(healthy['status'] == 'green' and not healthy.get('timed_out'), 'security fixture index unavailable')
            write_result(call(client, 'PUT', '/' + index + '/_doc/one?wait_for_active_shards=all', {'business_id': 'one', 'title': 'authorized', 'category': 'fixture', 'price': 1}))
        require(call(limited, 'GET', '/' + allowed + '/_doc/one')['found'], 'product least-privilege user cannot read its authorized index')
        call(limited, 'GET', '/' + denied + '/_doc/one', expected=(403,))
        wrong = connect(basic_auth=(username, 'invalid-' + prefix)); clients.append(wrong)
        call(wrong, 'GET', '/_security/_authenticate', expected=(401,))
        old_id, old_key = api_key()
        require(call(old_key, 'GET', '/' + allowed + '/_doc/one')['found'], 'scoped API key cannot read its authorized index')
        call(old_key, 'GET', '/' + denied + '/_doc/one', expected=(403,))
        call(old_key, 'PUT', '/' + allowed + '/_doc/denied-write', {'price': 2}, expected=(403,))
        new_id, new_key = api_key()
        require(call(new_key, 'GET', '/' + allowed + '/_doc/one')['found'], 'replacement API key failed before revocation')
        invalidated = call(limited, 'DELETE', '/_security/api_key', {'ids': [old_id], 'owner': True})
        require(old_id in invalidated['invalidated_api_keys'] and invalidated.get('error_count', 0) == 0, 'old key was not revoked')
        call(old_key, 'GET', '/' + allowed + '/_doc/one', expected=(401,))
        require(call(new_key, 'GET', '/' + allowed + '/_doc/one')['found'], 'replacement key stopped working after old-key revocation')
        endpoint = urlsplit(os.environ['ELASTICSEARCH_URL'])
        for url, ca in ((None, certifi.where()), ('https://' + socket.gethostbyname(endpoint.hostname) + ':9200', None)):
            candidate = connect(url=url, ca=ca); clients.append(candidate)
            certificate_refusal(candidate)
        nodes = call(client, 'GET', '/_nodes/settings?flat_settings=true')['nodes']
        require(nodes and all(str(n['settings'].get('xpack.security.transport.ssl.enabled')).lower() == 'true' for n in nodes.values()), 'transport TLS is not effective on every member')
        return {'case_ids': ['ES-24', 'ES-25'], 'api_key_rotation': 'new accepted before old revoked; old rejected; new accepted', 'tls_negative_probes': ['wrong CA', 'IP absent from service SAN']}
    finally:
        primary = sys.exc_info()[1]
        errors = []
        if keys:
            try:
                result = call(limited, 'DELETE', '/_security/api_key', {'ids': keys, 'owner': True})
                require(result.get('error_count', 0) == 0, 'API key cleanup failed')
            except Exception as error: errors.append(type(error).__name__)
        for index in reversed(created):
            try:
                call(client, 'DELETE', '/' + index, expected=(200, 404))
                call(client, 'GET', '/' + index, expected=(404,))
            except Exception as error: errors.append(type(error).__name__)
        for candidate in clients: candidate.close()
        if errors:
            if primary is None: raise ValueError('security fixture exact-name cleanup incomplete')
            print('ELASTICSEARCH_SECURITY_CLEANUP_FAIL count=' + str(len(errors)), file=sys.stderr)


def unavailable_write(client, prefix):
    """A fenced fixture uses an owned Pod IP and verifies the Service SAN.

    Service readiness may remove all Endpoints during this fault; DNS/timeout
    alone is not evidence that Elasticsearch safely rejected a write.
    """
    index, kind = os.environ['ELASTICSEARCH_FAULT_INDEX'], os.environ['ELASTICSEARCH_FAULT_KIND']
    require(re.fullmatch('es-gate-confirmed-[a-f0-9]{32}', index), 'fault index is not the owned confirmation ledger')
    require(kind in ('quorum', 'shard-loss', 'flood-watermark'), 'unexpected write-refusal scenario')
    if kind == 'quorum':
        deadline = time.monotonic()+120
        while time.monotonic() < deadline:
            state = call(client, 'GET', '/_cluster/health?master_timeout=5s', expected=(200, 503))
            if state.get('error', {}).get('type') == 'master_not_discovered_exception': break
            require('error' not in state, 'failure does not prove master unavailability')
            time.sleep(3)
        else: raise ValueError('quorum loss did not make the elected master unavailable')
    elif kind == 'shard-loss':
        deadline = time.monotonic()+120
        while time.monotonic() < deadline:
            state = call(client, 'GET', '/_cluster/health/' + index + '?timeout=5s')
            if state.get('status') == 'red': break
            time.sleep(3)
        else: raise ValueError('all-copy fault did not make the owned primary unavailable')
    else:
        settings = call(client, 'GET', '/'+index+'/_settings?flat_settings=true')
        require(str(settings[index]['settings'].get('index.blocks.read_only_allow_delete')).lower() == 'true',
                'watermark fault lacks the actual automatic index block')
    identifier = prefix + '-rejected'
    result = call(client, 'PUT', '/' + index + '/_create/' + identifier + '?timeout=5s',
                  {'business_id': identifier, 'value': -1}, expected=(429,) if kind == 'flood-watermark' else (503,))
    allowed = ({'cluster_block_exception', 'master_not_discovered_exception'} if kind == 'quorum' else
               {'cluster_block_exception'} if kind == 'flood-watermark' else {'unavailable_shards_exception'})
    require(result.get('error', {}).get('type') in allowed, 'write failure did not prove the declared safety boundary')
    return {'case_ids': [], 'fault': kind, 'rejected_id': identifier, 'write_http_status': 429 if kind == 'flood-watermark' else 503,
            'write_error_type': result['error']['type'], 'tls_hostname': os.environ['ELASTICSEARCH_ASSERT_HOSTNAME']}


def password_rotation(client, prefix):
    username = Path('/limited/username').read_text().strip()
    current = connect(basic_auth=(username, Path('/limited/password').read_text().strip()))
    previous = connect(basic_auth=(username, Path('/previous/password').read_text().strip()))
    try:
        require(call(current, 'GET', '/_security/_authenticate')['username'] == username,
                'rotated product user password is not effective')
        call(previous, 'GET', '/_security/_authenticate', expected=(401,))
        require(call(current, 'GET', '/_security/_authenticate')['username'] == username,
                'current password failed after checking old-password revocation')
        return {'case_ids': [], 'password_rotation': 'new authenticated; old rejected; new authenticated again'}
    finally:
        current.close(); previous.close()


def percentile(values, percent):
    ordered = sorted(values)
    require(bool(ordered), 'latency sample set is empty')
    offset = (len(ordered) - 1) * percent / 100
    lo, hi = int(offset), min(int(offset) + 1, len(ordered)-1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (offset - lo)


def confirmed_create(client, index, identifier, document, counters):
    from elastic_transport import ConnectionError, ConnectionTimeout
    # Requiring every configured copy intentionally blocks admission while a
    # deleted member rebuilds. Bound that recovery wait by both time and count;
    # six five-second admissions cannot cover the declared restart scenario.
    deadline = time.monotonic() + 300
    for attempt in range(40):
        if time.monotonic() >= deadline:
            break
        try:
            result = call(client, 'PUT', '/' + index + '/_create/' + identifier + '?wait_for_active_shards=all&timeout=5s', document, expected=(201, 409, 429, 503))
            if '_shards' in result:
                shards = result['_shards']
                if shards.get('failed') == 0 and shards.get('successful', 0) >= 1:
                    copies = str(shards['successful'])
                    counters['acknowledged_copies'][copies] = counters['acknowledged_copies'].get(copies, 0) + 1
                    return
                # Active-shard admission does not guarantee all copies survive
                # the operation. Do not count this as a complete acknowledgement;
                # resolve its stable ID below, within the same retry budget.
                counters['incomplete_shard_responses'] += 1
                print('ELASTICSEARCH_SOAK_WRITE_UNCONFIRMED successful=' + str(shards.get('successful')) +
                      ' failed=' + str(shards.get('failed')), flush=True)
            else:
                counters['rejected_or_conflict'] += 1
        except (ConnectionError, ConnectionTimeout):
            counters['unknown_outcomes'] += 1
        # Stable-ID readback precedes replay. create can safely refuse a late
        # earlier attempt; a conflict is accepted only after content matches.
        try:
            observed = call(client, 'GET', '/' + index + '/_doc/' + identifier, expected=(200, 404, 503))
            if observed.get('found'):
                require(observed['_source'] == document, 'stable-ID confirmation content differs')
                counters['confirmed_by_readback'] += 1
                return
        except (ConnectionError, ConnectionTimeout):
            counters['readback_unavailable'] += 1
        counters['retries'] += 1
        time.sleep(min(.25 * 2**attempt, 4))
    raise ValueError('bounded write confirmation exhausted during the declared fault')


def bounded_search(client, index, counters):
    """Read retries are safe, but partial shard responses are not success."""
    from elastic_transport import ConnectionError, ConnectionTimeout
    for attempt in range(6):
        try:
            result = call(client, 'POST', '/' + index + '/_search?allow_partial_search_results=false',
                          {'size': 5, 'query': {'match': {'title': 'distributed'}}}, expected=(200, 429, 503))
            shards = result.get('_shards', {})
            if not result.get('error') and not result.get('timed_out') and shards.get('failed') == 0 and shards.get('total', 0) > 0:
                return result
            counters['search_rejected_or_partial'] += 1
        except (ConnectionError, ConnectionTimeout):
            counters['search_unavailable'] += 1
        if attempt + 1 < 6:
            counters['search_retries'] += 1
            time.sleep(min(.25 * 2**attempt, 4))
    raise ValueError('bounded complete search retries exhausted during the declared fault')


def soak(client, prefix):
    index = prefix + '-soak'
    call(client, 'GET', '/' + index, expected=(404,))
    expected, write_latency, search_latency = {}, [], []
    counters = {'acknowledged_copies': {}, 'incomplete_shard_responses': 0, 'rejected_or_conflict': 0, 'unknown_outcomes': 0, 'confirmed_by_readback': 0, 'readback_unavailable': 0, 'retries': 0,
                'search_rejected_or_partial': 0, 'search_unavailable': 0, 'search_retries': 0}
    started = time.monotonic()
    try:
        body = mapping(); body['settings']['refresh_interval'] = '1s'
        call(client, 'PUT', '/' + index, body)
        health = call(client, 'GET', '/_cluster/health/' + index + '?wait_for_status=green&timeout=60s')
        require(health['status'] == 'green' and not health.get('timed_out'), 'soak index replicas unavailable')
        before = call(client, 'GET', '/_nodes/stats/jvm,process,fs,indices')
        started = time.monotonic(); next_heartbeat = started
        while time.monotonic() - started < 1800:
            iteration = time.monotonic()
            identifier = str(len(expected))
            document = {'business_id': identifier, 'title': 'steady distributed search', 'category': 'soak', 'price': len(expected)}
            confirmed_create(client, index, identifier, document, counters)
            write_latency.append((time.monotonic()-iteration)*1000)
            expected[identifier] = document
            query_started = time.monotonic()
            bounded_search(client, index, counters)
            search_latency.append((time.monotonic()-query_started)*1000)
            if time.monotonic() >= next_heartbeat:
                print('ELASTICSEARCH_SOAK_HEARTBEAT elapsed_seconds=' + str(round(time.monotonic()-started)) + ' confirmed=' + str(len(expected)), flush=True)
                next_heartbeat = time.monotonic() + 30
            remaining = 1 - (time.monotonic() - iteration)
            if remaining > 0: time.sleep(remaining)
        duration = time.monotonic() - started
        require(duration >= 1800, 'steady-state observation is shorter than 1800 seconds')
        healthy = call(client, 'GET', '/_cluster/health/' + index + '?wait_for_status=green&timeout=120s')
        require(healthy['status'] == 'green' and not healthy.get('timed_out'), 'soak did not recover its replicas')
        call(client, 'POST', '/' + index + '/_refresh')
        canonical_hash = read_documents(client, index, expected)
        after = call(client, 'GET', '/_nodes/stats/jvm,process,fs,indices')
        def resources(response):
            return {name: {'heap_used_bytes': n['jvm']['mem']['heap_used_in_bytes'], 'heap_max_bytes': n['jvm']['mem']['heap_max_in_bytes'],
                           'process_cpu_percent': n['process']['cpu']['percent'], 'filesystem_available_bytes': n['fs']['total']['available_in_bytes'],
                           'gc_collection_ms': sum(g['collection_time_in_millis'] for g in n['jvm']['gc']['collectors'].values())} for name, n in response['nodes'].items()}
        return {'case_ids': ['ES-49'], 'duration_seconds': duration, 'confirmed_documents': len(expected), 'canonical_hash': canonical_hash,
                'write_rate_per_second': len(expected)/duration, 'writes_ms': {str(p): percentile(write_latency,p) for p in (50,95,99)},
                'search_ms': {str(p): percentile(search_latency,p) for p in (50,95,99)}, 'retry_ledger': counters,
                'resources_before': resources(before), 'resources_after': resources(after), 'load_shape': 'one stable-ID create and one search per second; small fixture documents'}
    finally:
        primary = sys.exc_info()[1]
        try:
            call(client, 'DELETE', '/' + index, expected=(200, 404))
            call(client, 'GET', '/' + index, expected=(404,))
        except Exception as error:
            if primary is None: raise
            print('ELASTICSEARCH_SOAK_CLEANUP_FAIL reason=' + type(error).__name__, file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('module', choices=['business', 'lifecycle', 'security', 'password-rotation', 'unavailable-write', 'soak'])
    parser.add_argument('--token', required=True)
    args = parser.parse_args()
    require(re.fullmatch(r'[a-f0-9]{16}', args.token), 'fixture token must be an exact owned identifier')
    client = connect()
    try:
        result = globals()[args.module.replace('-', '_')](client, 'es-gate-' + args.token)
    finally:
        client.close()
    print('ELASTICSEARCH_BUSINESS_MEASUREMENT ' + json.dumps(result, sort_keys=True), flush=True)
    print('ELASTICSEARCH_BUSINESS_SCOPED_CLEAN_PASS module=' + args.module, flush=True)
    for case in result['case_ids']:
        print('ELASTICSEARCH_CASE_PASS id=' + case, flush=True)


def report_failure(error):
    # ApiError may contain credentials. Report only this script's code
    # locations, never exception text, response bodies, locals or source lines.
    locations = []
    frame = error.__traceback__
    while frame is not None:
        if frame.tb_frame.f_code.co_filename == __file__:
            locations.append(frame.tb_frame.f_code.co_name + ':' + str(frame.tb_lineno))
        frame = frame.tb_next
    causes, seen = [], set()
    cause = error.__cause__ or error.__context__
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        causes.append(type(cause).__name__)
        cause = cause.__cause__ or cause.__context__
    print('ELASTICSEARCH_BUSINESS_FAIL reason=' + type(error).__name__ +
          ' location=' + ','.join(locations) + ' causes=' + ','.join(causes), file=sys.stderr)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        report_failure(exc)
        raise SystemExit(1)
