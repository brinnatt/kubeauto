"""Kafka fixture supply-chain and initial-voter format contracts."""
import hashlib
import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('kafka_prepare', ROOT/'tests/helpers/kafka-fixture-prepare.py')
PREP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREP)


class KafkaFixtureContracts(unittest.TestCase):
    def test_download_rejects_partial_or_corrupt_sources_before_atomic_replace(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);good=base/'good';good.write_bytes(b'verified archive')
            bad=base/'bad';bad.write_bytes(b'partial')
            target=base/'cache/archive';target.parent.mkdir();target.write_bytes(b'previous')
            expected=hashlib.sha512(good.read_bytes()).hexdigest()
            with self.assertRaisesRegex(ValueError,'UNAVAILABLE'):
                PREP.download(target,[bad.as_uri()],expected)
            self.assertEqual(target.read_bytes(),b'previous')
            self.assertEqual(list(target.parent.iterdir()),[target])
            PREP.download(target,[(base/'missing').as_uri(),good.as_uri()],expected)
            self.assertEqual(target.read_bytes(),good.read_bytes())
            # A verified cached archive is reusable without a network request.
            PREP.download(target,[(base/'missing').as_uri()],expected)

    def test_archive_layout_and_path_escape_are_checked_before_extraction(self):
        for invalid in ('../outside','/tmp/outside',PREP.ARCHIVE[:-4]+'/../../outside','link'):
            with self.subTest(invalid=invalid),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);archive=root/'archive.tgz';dest=root/'kafka'
                with tarfile.open(archive,'w:gz') as bundle:
                    item=tarfile.TarInfo(invalid)
                    if invalid=='link':item.type=tarfile.SYMTYPE;item.linkname='/tmp/outside'
                    bundle.addfile(item)
                with self.assertRaisesRegex(ValueError,'MEMBER_REFUSED'):PREP.unpack(archive,dest)
                self.assertFalse(dest.exists())
                self.assertEqual(list(root.iterdir()),[archive])
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);archive=root/'archive.tgz';dest=root/'kafka'
            with tarfile.open(archive,'w:gz') as bundle:
                item=tarfile.TarInfo(PREP.ARCHIVE[:-4]+'/bin/kafka-storage.sh');item.size=2;item.mode=0o755
                bundle.addfile(item,io.BytesIO(b'#!'))
            PREP.unpack(archive,dest)
            self.assertEqual((dest/'bin/kafka-storage.sh').read_bytes(),b'#!')
            with self.assertRaisesRegex(ValueError,'DESTINATION_EXISTS'):PREP.unpack(archive,dest)

    def test_native_voter_fixture_reads_back_cluster_node_and_directory_id(self):
        text=(ROOT/'tests/helpers/kafka-cli-live-regression.sh').read_text()
        self.assertIn('--initial-controllers "$VOTERS"',text)
        initial=text.split('KAFKA_CLI_LIVE_STAGE initial-voters-format',1)[1].split('KAFKA_INITIAL_VOTERS_FORMAT_PASS',1)[0]
        self.assertIn('--controller-listen-host 0.0.0.0',initial)
        self.assertNotIn('--controller-listen-host 127.0.0.1',initial)
        for name in ('cluster.id','node.id','directory.id'):
            self.assertIn(name,text)
        self.assertIn('KAFKA_INITIAL_VOTERS_FORMAT_PASS',text)
        runner=(ROOT/'tests/run_tools_regression.sh').read_text()
        branch=runner.split('  --kafka-live)\n',1)[1].split('\n    ;;',1)[0]
        self.assertIn('kafka-fixture-prepare.py',branch)
        self.assertNotIn('docker create',branch)
        self.assertIn('192.168.122.243',branch)
        self.assertNotIn('192.168.122.217',branch)
