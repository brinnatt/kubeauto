#!/usr/bin/env python3
"""Stage the pinned Apache Kafka distribution for the tools-owned fixture."""
import hashlib
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import urllib.request

VERSION = '4.3.1'
ARCHIVE = 'kafka_2.13-' + VERSION + '.tgz'
SHA512 = ('c7d7b2318cb51aa0c61d3246a51c349210073c5c9b754947ef965a439f2f939e'
          '8600f204e134a75ac31faf3829c9370960ef7c6a9886c8a1dbf0339a21f4c54c')
SOURCES = ('https://mirrors.huaweicloud.com/apache/kafka/' + VERSION + '/' + ARCHIVE,
           'https://downloads.apache.org/kafka/' + VERSION + '/' + ARCHIVE)


def sha512(path):
    digest = hashlib.sha512()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def download(target, sources, expected):
    if target.is_file() and sha512(target) == expected:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    for url in sources:
        partial = None
        try:
            with tempfile.NamedTemporaryFile(dir=str(target.parent), delete=False) as output:
                partial = Path(output.name)
                with urllib.request.urlopen(url, timeout=45) as response:
                    shutil.copyfileobj(response, output, length=1024 * 1024)
            if sha512(partial) != expected:
                raise ValueError('KAFKA_ARCHIVE_CHECKSUM_MISMATCH')
            os.replace(str(partial), str(target))
            print('KAFKA_ARCHIVE_READY source=' + url + ' sha512=' + expected, flush=True)
            return
        except (OSError, ValueError) as error:
            print('KAFKA_ARCHIVE_SOURCE_REFUSED source=' + url + ' reason=' + str(error), flush=True)
        finally:
            if partial is not None and partial.exists():
                partial.unlink()
    raise ValueError('KAFKA_ARCHIVE_UNAVAILABLE')


def unpack(archive, destination):
    if destination.exists():
        raise ValueError('KAFKA_FIXTURE_DESTINATION_EXISTS')
    top = ARCHIVE[:-4]
    with tempfile.TemporaryDirectory(prefix='.kafka-extract-', dir=str(destination.parent)) as directory:
        with tarfile.open(str(archive), 'r:gz') as bundle:
            members = bundle.getmembers()
            # The official distribution has regular files and directories only.
            # Refuse all links, special files and traversal before writing any member.
            for member in members:
                parts = Path(member.name).parts
                if (not parts or parts[0] != top or '..' in parts or
                        not (member.isfile() or member.isdir())):
                    raise ValueError('KAFKA_ARCHIVE_MEMBER_REFUSED ' + member.name)
            bundle.extractall(directory, members=members)
        source = Path(directory) / top
        if not (source / 'bin/kafka-storage.sh').is_file():
            raise ValueError('KAFKA_ARCHIVE_LAYOUT_REFUSED')
        source.rename(destination)


def main():
    if len(sys.argv) != 2 or sys.argv[1] != '/tmp/kafka-cli-multi':
        raise ValueError('KAFKA_FIXTURE_SCOPE_REFUSED')
    root = Path(sys.argv[1])
    archive = Path('/var/cache/kubeauto-tools') / ARCHIVE
    download(archive, SOURCES, SHA512)
    unpack(archive, root / 'kafka')
    print('KAFKA_OFFICIAL_BINARY_PASS version=' + VERSION + ' sha512=' + SHA512, flush=True)


if __name__ == '__main__':
    main()
