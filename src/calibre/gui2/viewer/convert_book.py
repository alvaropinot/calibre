#!/usr/bin/env python2
# vim:fileencoding=utf-8
# License: GPL v3 Copyright: 2018, Kovid Goyal <kovid at kovidgoyal.net>

from __future__ import absolute_import, division, print_function, unicode_literals

import errno
import json
import os
import shutil
import tempfile
import time
from hashlib import sha1

from calibre import walk
from calibre.constants import cache_dir, iswindows
from calibre.srv.render_book import RENDER_VERSION
from calibre.utils.ipc.simple_worker import fork_job
from calibre.utils.lock import ExclusiveFile
from calibre.utils.short_uuid import uuid4
from polyglot.builtins import as_bytes, as_unicode, iteritems

DAY = 24 * 3600
VIEWER_VERSION = 1


def book_cache_dir():
    return getattr(book_cache_dir, 'override', os.path.join(cache_dir(), 'ev2'))


def cache_lock():
    return ExclusiveFile(os.path.join(book_cache_dir(), 'metadata.json'))


def book_hash(path, size, mtime):
    path = os.path.normcase(os.path.abspath(path))
    raw = json.dumps((path, size, mtime, RENDER_VERSION, VIEWER_VERSION))
    if not isinstance(raw, bytes):
        raw = raw.encode('utf-8')
    return as_unicode(sha1(raw).hexdigest())


def safe_makedirs(path):
    try:
        os.makedirs(path)
    except EnvironmentError as err:
        if err.errno != errno.EEXIST:
            raise
    return path


def robust_rmtree(x):
    retries = 2 if iswindows else 1  # retry on windows to get around the idiotic mandatory file locking
    for i in range(retries):
        try:
            shutil.rmtree(x)
            return True
        except EnvironmentError:
            time.sleep(0.1)
    return False


def robust_rename(a, b):
    retries = 20 if iswindows else 1  # retry on windows to get around the idiotic mandatory file locking
    for i in range(retries):
        try:
            os.rename(a, b)
            return True
        except EnvironmentError:
            time.sleep(0.1)
    return False


def clear_temp(temp_path):
    now = time.time()
    for x in os.listdir(temp_path):
        x = os.path.join(temp_path, x)
        mtime = os.path.getmtime(x)
        if now - mtime > DAY:
            robust_rmtree(x)


def expire_cache(path, instances, max_age):
    now = time.time()
    remove = [x for x in instances if now - x['atime'] > max_age and x['status'] == 'finished']
    for instance in remove:
        if robust_rmtree(os.path.join(path, instance['path'])):
            instances.remove(instance)


def expire_cache_and_temp(temp_path, finished_path, metadata, max_age):
    now = time.time()
    if now - metadata['last_clear_at'] < DAY and max_age >= 0:
        return
    clear_temp(temp_path)
    entries = metadata['entries']
    for key, instances in tuple(entries.items()):
        if instances:
            expire_cache(finished_path, instances, max_age)
            if not instances:
                del entries[key]
    metadata['last_clear_at'] = now


def prepare_convert(temp_path, key, st):
    tdir = tempfile.mkdtemp(dir=temp_path)
    now = time.time()
    return {
        'path': os.path.basename(tdir),
        'id': uuid4(),
        'status': 'working',
        'mtime': now,
        'atime': now,
        'key': key,
        'file_mtime': st.st_mtime,
        'file_size': st.st_size,
        'cache_size': 0,
    }


def do_convert(path, temp_path, key, instance):
    tdir = os.path.join(temp_path, instance['path'])
    fork_job('calibre.srv.render_book', 'render_for_viewer', args=(
        path, tdir, {'size': instance['file_size'], 'mtime': instance['file_mtime'], 'hash': key},
        ), timeout=3000, no_output=True
    )
    size = 0
    for f in walk(tdir):
        size += os.path.getsize(f)
    instance['cache_size'] = size


def save_metadata(metadata, f):
    f.seek(0), f.truncate(), f.write(as_bytes(json.dumps(metadata, indent=2)))


def prepare_book(path, convert_func=do_convert, max_age=30 * DAY, force=False, prepare_notify=None):
    st = os.stat(path)
    key = book_hash(path, st.st_size, st.st_mtime)
    finished_path = safe_makedirs(os.path.join(book_cache_dir(), 'f'))
    temp_path = safe_makedirs(os.path.join(book_cache_dir(), 't'))

    with cache_lock() as f:
        try:
            metadata = json.loads(f.read())
        except ValueError:
            metadata = {'entries': {}, 'last_clear_at': 0}
        entries = metadata['entries']
        instances = entries.setdefault(key, [])
        for instance in tuple(instances):
            if instance['status'] == 'finished':
                if force:
                    robust_rmtree(os.path.join(finished_path, instance['path']))
                    instances.remove(instance)
                else:
                    instance['atime'] = time.time()
                    save_metadata(metadata, f)
                    return os.path.join(finished_path, instance['path'])
        if prepare_notify:
            prepare_notify()
        instance = prepare_convert(temp_path, key, st)
        instances.append(instance)
        save_metadata(metadata, f)
    convert_func(path, temp_path, key, instance)
    src_path = os.path.join(temp_path, instance['path'])
    with cache_lock() as f:
        ans = tempfile.mkdtemp(dir=finished_path)
        instance['path'] = os.path.basename(ans)
        try:
            metadata = json.loads(f.read())
        except ValueError:
            metadata = {'entries': {}, 'last_clear_at': 0}
        entries = metadata['entries']
        instances = entries.setdefault(key, [])
        os.rmdir(ans)
        if not robust_rename(src_path, ans):
            raise Exception((
                'Failed to rename: "{}" to "{}" probably some software such as an antivirus or file sync program'
                ' running on your computer has locked the files'
            ).format(src_path, ans))

        instance['status'] = 'finished'
        for q in instances:
            if q['id'] == instance['id']:
                q.update(instance)
                break
        expire_cache_and_temp(temp_path, finished_path, metadata, max_age)
        save_metadata(metadata, f)
    return ans


def update_book(path, old_stat, name_data_map=None):
    old_key = book_hash(path, old_stat.st_size, old_stat.st_mtime)
    finished_path = safe_makedirs(os.path.join(book_cache_dir(), 'f'))

    with cache_lock() as f:
        st = os.stat(path)
        new_key = book_hash(path, st.st_size, st.st_mtime)
        if old_key == new_key:
            return
        try:
            metadata = json.loads(f.read())
        except ValueError:
            metadata = {'entries': {}, 'last_clear_at': 0}
        entries = metadata['entries']
        instances = entries.get(old_key)
        if not instances:
            return
        for instance in tuple(instances):
            if instance['status'] == 'finished':
                entries.setdefault(new_key, []).append(instance)
                instances.remove(instance)
                if not instances:
                    del entries[old_key]
                instance['file_mtime'] = st.st_mtime
                instance['file_size'] = st.st_size
                if name_data_map:
                    for name, data in iteritems(name_data_map):
                        with open(os.path.join(finished_path, instance['path'], name), 'wb') as f2:
                            f2.write(data)
                save_metadata(metadata, f)
                return


def find_tests():
    import unittest

    class TestViewerCache(unittest.TestCase):
        ae = unittest.TestCase.assertEqual

        def setUp(self):
            self.tdir = tempfile.mkdtemp()
            book_cache_dir.override = os.path.join(self.tdir, 'ev2')

        def tearDown(self):
            shutil.rmtree(self.tdir)
            del book_cache_dir.override

        def test_viewer_cache(self):

            def convert_mock(path, temp_path, key, instance):
                self.ae(instance['status'], 'working')
                self.ae(instance['key'], key)
                open(os.path.join(temp_path, instance['path'], 'sentinel'), 'wb').write(b'test')

            book_src = os.path.join(self.tdir, 'book.epub')
            open(book_src, 'wb').write(b'a')
            path = prepare_book(book_src, convert_func=convert_mock)
            self.ae(open(os.path.join(path, 'sentinel'), 'rb').read(), b'test')

            # Test that opening the same book uses the cache
            second_path = prepare_book(book_src, convert_func=convert_mock)
            self.ae(path, second_path)

            # Test that changing the book updates the cache
            open(book_src, 'wb').write(b'bc')
            third_path = prepare_book(book_src, convert_func=convert_mock)
            self.assertNotEqual(path, third_path)

            # Test force reload
            fourth_path = prepare_book(book_src, convert_func=convert_mock)
            self.ae(third_path, fourth_path)
            fourth_path = prepare_book(book_src, convert_func=convert_mock, force=True)
            self.assertNotEqual(third_path, fourth_path)

            # Test cache expiry
            open(book_src, 'wb').write(b'bcd')
            prepare_book(book_src, convert_func=convert_mock, max_age=-1000)
            self.ae([], os.listdir(os.path.join(book_cache_dir(), 'f')))

            # Test updating cached book
            book_src = os.path.join(self.tdir, 'book2.epub')
            open(book_src, 'wb').write(b'bb')
            path = prepare_book(book_src, convert_func=convert_mock)
            self.ae(open(os.path.join(path, 'sentinel'), 'rb').read(), b'test')
            bs = os.stat(book_src)
            open(book_src, 'wb').write(b'cde')
            update_book(book_src, bs, name_data_map={'sentinel': b'updated'})
            self.ae(open(os.path.join(path, 'sentinel'), 'rb').read(), b'updated')
            self.ae(1, len(os.listdir(os.path.join(book_cache_dir(), 'f'))))
            with cache_lock() as f:
                metadata = json.loads(f.read())
            self.ae(len(metadata['entries']), 1)
            entry = list(metadata['entries'].values())[0]
            self.ae(len(entry), 1)
            entry = entry[0]
            st = os.stat(book_src)
            self.ae(entry['file_size'], st.st_size)
            self.ae(entry['file_mtime'], st.st_mtime)

    return unittest.defaultTestLoader.loadTestsFromTestCase(TestViewerCache)
