"""Exercise contention, nested lock order and process exclusion."""
import os
from pathlib import Path
import subprocess
import sys
import threading
from unittest import mock

import pytest

import file_lock


def test_unrelated_files_do_not_share_a_critical_section(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    other = threading.Event()

    def first():
        with file_lock.guard(tmp_path / 'one.lock'):
            entered.set()
            release.wait(5)

    def second():
        with file_lock.guard(tmp_path / 'two.lock'):
            other.set()

    a, b = threading.Thread(target=first), threading.Thread(target=second)
    a.start()
    try:
        assert entered.wait(2)
        b.start()
        assert other.wait(2), 'Unrelated file was serialized behind the first'
    finally:
        release.set()
        a.join(5)
        if b.ident is not None:
            b.join(5)
    assert not a.is_alive() and not b.is_alive()


def test_same_file_is_exclusive_and_reentrant(tmp_path):
    attempted, entered = threading.Event(), threading.Event()

    def contender():
        attempted.set()
        with file_lock.guard(tmp_path / 'same.lock'):
            entered.set()

    worker = threading.Thread(target=contender)
    try:
        with file_lock.guard(tmp_path / 'same.lock'):
            with file_lock.guard(tmp_path / '.' / 'same.lock'):
                worker.start()
                assert attempted.wait(2)
                assert not entered.wait(.1)
        assert entered.wait(2)
    finally:
        if worker.ident is not None:
            worker.join(5)
    assert not worker.is_alive()


def test_nested_order_is_enforced_before_waiting(tmp_path):
    with file_lock.guard(tmp_path / 'store', rank=file_lock.STORE):
        with file_lock.guard(tmp_path / 'session', rank=file_lock.SESSION):
            with file_lock.guard(tmp_path / 'claim', rank=file_lock.CLAIM):
                with pytest.raises(file_lock.LockOrderError):
                    with file_lock.guard(tmp_path / 'other-store'):
                        pytest.fail('Inversion admitted')
                with file_lock.guard(tmp_path / 'store'):
                    pass  # Reentering an already held lock cannot create ABBA.
    with file_lock.guard(tmp_path / 'b'):
        with pytest.raises(file_lock.LockOrderError):
            with file_lock.guard(tmp_path / 'a'):
                pytest.fail('Same-rank inversion admitted')
    # Failure did not poison the thread-local acquisition stack.
    with file_lock.guard(tmp_path / 'a'):
        with file_lock.guard(tmp_path / 'b'):
            pass


@pytest.mark.skipif(os.name == 'nt', reason='POSIX flock contention')
def test_blocked_flock_does_not_block_unrelated_threads(tmp_path):
    import fcntl
    busy = tmp_path / 'busy'
    holder = subprocess.Popen([sys.executable, '-B', '-c',
        'import fcntl,sys; f=open(sys.argv[1],"a+b"); '
        'fcntl.flock(f,fcntl.LOCK_EX); print("held",flush=True); sys.stdin.readline()',
        str(busy)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    attempted, entered, other = threading.Event(), threading.Event(), threading.Event()
    real = fcntl.flock

    def observed(fd, operation):
        if operation == fcntl.LOCK_EX and threading.current_thread().name == 'blocked-flock':
            attempted.set()
        return real(fd, operation)

    def blocked():
        with file_lock.guard(busy):
            entered.set()

    def independent():
        with file_lock.guard(tmp_path / 'free'):
            other.set()

    a = threading.Thread(target=blocked, name='blocked-flock')
    b = threading.Thread(target=independent)
    try:
        assert holder.stdout.readline().strip() == 'held'
        with mock.patch.object(fcntl, 'flock', side_effect=observed):
            a.start()
            assert attempted.wait(2)
            b.start()
            assert other.wait(2), 'Global mutex was held while waiting for flock'
            assert not entered.is_set()
            holder.communicate('\n', timeout=5)
            a.join(5)
            b.join(5)
    finally:
        if holder.poll() is None:
            holder.kill()
        holder.communicate(timeout=5)
        for worker in (a, b):
            if worker.ident is not None:
                worker.join(5)
    assert entered.is_set()
    assert not a.is_alive() and not b.is_alive()


def test_opposite_process_lock_orders_fail_fast(tmp_path):
    code = '''
import sys,time
from pathlib import Path
import file_lock
root=Path(sys.argv[1]); first,second=sys.argv[2:]
with file_lock.guard(root/first):
    (root/(first+'.ready')).touch()
    deadline=time.monotonic()+5
    while not (root/(second+'.ready')).exists():
        if time.monotonic()>deadline: raise RuntimeError('peer never entered')
        time.sleep(.01)
    try:
        with file_lock.guard(root/second): print('complete',flush=True)
    except file_lock.LockOrderError: print('inversion',flush=True)
'''
    workers = [subprocess.Popen([sys.executable, '-B', '-c', code, str(tmp_path), a, b],
               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
               for a, b in [('a', 'b'), ('b', 'a')]]
    try:
        results = [p.communicate(timeout=10) for p in workers]
        assert [p.returncode for p in workers] == [0, 0], results
        assert [out.strip() for out, _err in results] == ['complete', 'inversion']
    finally:
        for p in workers:
            if p.poll() is None:
                p.kill()
            p.communicate(timeout=5)
