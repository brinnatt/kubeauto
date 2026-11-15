"""Durable, exclusively owned progress for one frozen minor-upgrade plan.

The caller must reobserve the cluster before resuming an interrupted step.
This journal records completion; it does not prove health or authorize rollback.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import tempfile


class JournalRefused(ValueError):
    """Another owner, changed inputs or invalid progress prevents execution."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def private_file(path: Path, flags: int) -> int:
    fd = os.open(path, flags | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or
            stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        os.close(fd)
        raise JournalRefused('journal files must be private regular files owned by this user')
    return fd


class Journal:
    def __init__(self, directory: Path, binding: dict, steps: list[str]):
        # Use one cluster-owned directory, shared by all run IDs, so two plans
        # cannot evade mutual exclusion by choosing different run subdirectories.
        self.directory = Path(directory)
        self.binding = deepcopy(binding)
        self.steps = list(steps)
        self.fd = None
        self.data = None
        if (not isinstance(binding, dict) or not binding or not steps or
                len(steps) != len(set(steps)) or
                not all(isinstance(s, str) and re.fullmatch(r'[a-zA-Z0-9_.:/-]{1,160}', s)
                        for s in steps)):
            raise JournalRefused('a nonempty binding and unique ordered steps are required')

    def __enter__(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or
                stat.S_IMODE(info.st_mode) != 0o700):
            raise JournalRefused('journal directory must be private and owned by this user')
        self.fd = private_file(self.directory / 'owner.lock', os.O_RDWR | os.O_CREAT)
        try:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise JournalRefused('this cluster already has an active upgrade owner') from None
            path = self.directory / 'journal.json'
            if path.exists() or path.is_symlink():
                with os.fdopen(private_file(path, os.O_RDONLY)) as stream:
                    self.data = json.load(stream)
                self._validate()
            else:
                self.data = {'schema_version': 1, 'binding': self.binding, 'steps': self.steps,
                             'completed': [], 'active': None, 'failures': [], 'updated_at': now()}
                self._save()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *_):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def _validate(self):
        value = self.data
        if (not isinstance(value, dict) or value.get('schema_version') != 1 or
                value.get('binding') != self.binding or value.get('steps') != self.steps):
            raise JournalRefused('journal cluster identity, inputs or frozen plan differ')
        completed = value.get('completed')
        if (not isinstance(completed, list) or completed != self.steps[:len(completed)] or
                len(completed) > len(self.steps) or not isinstance(value.get('failures'), list)):
            raise JournalRefused('journal completion is not a valid plan prefix')
        next_step = self.steps[len(completed)] if len(completed) < len(self.steps) else None
        if value.get('active') not in (None, next_step):
            raise JournalRefused('journal active step is outside the next allowed boundary')

    def _save(self):
        if self.fd is None:
            raise JournalRefused('journal owner lock is not held')
        self._validate()
        self.data['updated_at'] = now()
        with tempfile.NamedTemporaryFile(mode='w', dir=self.directory, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                json.dump(self.data, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
                os.replace(temporary, self.directory / 'journal.json')
                directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                temporary.unlink(missing_ok=True)

    def begin(self, step: str):
        self._validate()
        if (len(self.data['completed']) == len(self.steps) or
                step != self.steps[len(self.data['completed'])]):
            raise JournalRefused('only the next incomplete step may begin or resume')
        self.data['active'] = step
        self._save()

    def complete(self, step: str):
        if self.data['active'] != step:
            raise JournalRefused('only the active step can be accepted')
        self.data['completed'].append(step)
        self.data['active'] = None
        self._save()

    def failed(self, step: str, rc: int, category: str):
        if (self.data['active'] != step or type(rc) is not int or rc == 0 or
                category not in {'product', 'test-gate', 'environment', 'supply-chain',
                                 'runtime', 'kubernetes', 'unclassified'}):
            raise JournalRefused('a failure must bind the active step, nonzero rc and category')
        # Never replace the first failure or store raw command output/secrets.
        self.data['failures'].append({'step': step, 'rc': rc, 'category': category, 'at': now()})
        self._save()
