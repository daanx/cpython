import unittest

import threading
from threading import Thread
import textwrap
import time
from unittest import TestCase
import gc
import weakref

from test import support
from test.support import import_helper, script_helper, threading_helper


class MyObj:
    pass


@threading_helper.requires_working_threading()
class TestGC(TestCase):
    @support.requires_subprocess()
    def test_mimalloc_abandoned_page_owner(self):
        for full_retain in (0, 2):
            with self.subTest(page_full_retain=full_retain):
                self.check_mimalloc_page_owner(
                    reclaim=False, full_retain=full_retain)

    @support.requires_subprocess()
    def test_mimalloc_reclaimed_page_owner(self):
        for full_retain in (0, 2):
            with self.subTest(page_full_retain=full_retain):
                self.check_mimalloc_page_owner(
                    reclaim=True, full_retain=full_retain)

    def check_mimalloc_page_owner(self, *, reclaim, full_retain):
        import_helper.import_module('_testinternalcapi')
        script = textwrap.dedent(f"""
            from collections import Counter
            from concurrent.futures import ThreadPoolExecutor
            import faulthandler
            import gc
            import threading
            import weakref

            from _testinternalcapi import object_thread_id, py_thread_id

            faulthandler.dump_traceback_later({support.SHORT_TIMEOUT}, exit=True)
            gc.disable()

            class Node:
                __slots__ = ("value", "link", "__weakref__")

                def __init__(self, value):
                    self.value = value
                    self.link = None

            count = 20_000
            state = {{}}
            ready = threading.Event()
            reclaimed = threading.Event()
            finish = threading.Event()

            def allocate():
                state["owner"] = py_thread_id()
                objects = [Node(i) for i in range(count)]
                state["freed"] = {{id(objects[i])
                                  for i in range(0, count, 2)}}
                state["garbage"] = [weakref.ref(objects[i])
                                    for i in range(0, count, 2)]
                for i in range(0, count, 2):
                    objects[i].link = objects[i]
                    objects[i] = None
                state["objects"] = objects
                ready.set()
                assert finish.wait({support.SHORT_TIMEOUT})
                return sum(obj.value for obj in objects if obj is not None)

            def reclaim_pages():
                state["reclaimer"] = py_thread_id()
                objects = [Node(i) for i in range(count)]
                state["reused"] = sum(id(obj) in state["freed"]
                                      for obj in objects)
                state["new"] = objects
                reclaimed.set()
                assert finish.wait({support.SHORT_TIMEOUT})
                return sum(obj.value for obj in state["objects"]
                           if obj is not None)

            expected_sum = sum(range(1, count, 2))
            with ThreadPoolExecutor(max_workers=2) as pool:
                owner = pool.submit(allocate)
                other = None
                try:
                    assert ready.wait({support.SHORT_TIMEOUT})
                    objects = state["objects"]
                    refs = [weakref.ref(obj) for obj in objects
                            if obj is not None]
                    assert {{object_thread_id(obj) for obj in objects
                            if obj is not None}} == {{state["owner"]}}
                    # Collect cycles on a different thread to avoid owner reclaim.
                    gc.collect()
                    assert all(ref() is None for ref in state["garbage"])
                    expected_owners = {{state["owner"]}}
                    if {reclaim!r}:
                        other = pool.submit(reclaim_pages)
                        assert reclaimed.wait({support.SHORT_TIMEOUT})
                        assert state["reused"] > 0, "no freed slots were reused"
                        expected_owners.add(state["reclaimer"])
                    for _ in range(2):
                        gc.collect()
                        owners = Counter(object_thread_id(obj)
                                         for obj in objects if obj is not None)
                        assert owners.keys() <= expected_owners, owners
                        if {reclaim!r}:
                            assert owners[state["reclaimer"]] > 0, owners
                        assert sum(obj.value for obj in objects
                                   if obj is not None) == expected_sum
                finally:
                    finish.set()
                assert owner.result() == expected_sum
                if other is not None:
                    assert other.result() == expected_sum
            del objects
            state.clear()
            gc.collect()
            assert all(ref() is None for ref in refs)
            faulthandler.cancel_dump_traceback_later()
        """)
        script_helper.assert_python_ok(
            "-X", "gil=0", "-c", script,
            MIMALLOC_PAGE_FULL_RETAIN=str(full_retain),
            MIMALLOC_PAGE_RECLAIM_ON_FREE="0",
        )

    def test_collect_cycles_after_thread_exit(self):
        class Slotted:
            __slots__ = ("cycle", "__weakref__")

        refs = []

        def worker():
            for cls in (MyObj, Slotted):
                for _ in range(200):
                    obj = cls()
                    obj.cycle = obj
                    refs.append(weakref.ref(obj))
            obj = Slotted()
            obj.cycle = (obj,) * 100_000
            refs.append(weakref.ref(obj))

        with support.disable_gc():
            with threading_helper.start_threads([Thread(target=worker)]):
                pass
            self.assertTrue(all(ref() is not None for ref in refs))
            gc.collect()
            self.assertTrue(all(ref() is None for ref in refs))

    def test_get_objects(self):
        event = threading.Event()

        def gc_thread():
            for i in range(100):
                o = gc.get_objects()
            event.set()

        def mutator_thread():
            while not event.is_set():
                o1 = MyObj()
                o2 = MyObj()
                o3 = MyObj()
                o4 = MyObj()

        gcs = [Thread(target=gc_thread)]
        mutators = [Thread(target=mutator_thread) for _ in range(4)]
        with threading_helper.start_threads(gcs + mutators):
            pass

    def test_get_referrers(self):
        NUM_GC = 2
        NUM_MUTATORS = 4

        b = threading.Barrier(NUM_GC + NUM_MUTATORS)
        event = threading.Event()

        obj = MyObj()

        def gc_thread():
            b.wait()
            for i in range(100):
                o = gc.get_referrers(obj)
            event.set()

        def mutator_thread():
            b.wait()
            while not event.is_set():
                d1 = { "key": obj }
                d2 = { "key": obj }
                d3 = { "key": obj }
                d4 = { "key": obj }

        gcs = [Thread(target=gc_thread) for _ in range(NUM_GC)]
        mutators = [Thread(target=mutator_thread) for _ in range(NUM_MUTATORS)]
        with threading_helper.start_threads(gcs + mutators):
            pass

    def test_freeze_object_in_brc_queue(self):
        # GH-142975: Freezing objects in the BRC queue could result in some
        # objects having a zero refcount without being deallocated.

        class Weird:
            # We need a destructor to trigger the check for object resurrection
            def __del__(self):
                pass

        # This is owned by the main thread, so the subthread will have to increment
        # this object's reference count.
        weird = Weird()

        def evil():
            gc.freeze()

            # Decrement the reference count from this thread, which will trigger the
            # slow path during resurrection and add our weird object to the BRC queue.
            nonlocal weird
            del weird

            # Collection will merge the object's reference count and make it zero.
            gc.collect()

            # Unfreeze the object, making it visible to the GC.
            gc.unfreeze()
            gc.collect()

        thread = Thread(target=evil)
        thread.start()
        thread.join()

    def test_running_method_not_collected(self):
        class C:
            def m(self):
                wr = weakref.ref(C.__dict__["m"])
                del C.m
                gc.collect()
                return wr() is not None

            def run(self):
                return self.m()

        self.assertTrue(C().run())

    def test_merge_brc_queue_of_detached_thread(self):
        # GH-157838: objects queued for merging by a thread that is detached
        # (blocked in a lock acquire, sleep, etc.) are merged and freed on its
        # behalf instead of staying alive until it runs Python code again.
        lock = threading.Lock()
        lock.acquire()
        ready = threading.Event()
        objs = []

        def worker():
            # Objects owned by this thread; only the list holds a reference.
            objs.extend(MyObj() for _ in range(100))
            ready.set()
            lock.acquire()  # block while detached

        thread = Thread(target=worker)
        thread.start()
        try:
            ready.wait()
            # The worker may not have detached yet when the first objects
            # are dropped; keep trying until one is freed immediately.
            for _ in support.sleeping_retry(support.SHORT_TIMEOUT, error=False):
                obj = objs.pop()
                wr = weakref.ref(obj)
                del obj
                if wr() is None:
                    break
            else:
                self.fail("object not freed while owning thread was detached")
        finally:
            lock.release()
            thread.join()

    def test_gc_callbacks_race_with_mutation(self):
        def collect():
            b.wait()
            while not stop.is_set():
                gc.collect()

        def mutate():
            b.wait()
            while not stop.is_set():
                gc.callbacks[:] = [lambda *_: _ for _ in range(16)]
                time.sleep(0)
                gc.callbacks.clear()

        threads = [threading.Thread(target=f) for f in (collect, mutate) * 4]
        b = threading.Barrier(len(threads) + 1)
        stop = threading.Event()

        with threading_helper.start_threads(threads, stop.set):
            b.wait()
            time.sleep(0.2)
    def test_set_threshold(self):
        # GH-148613: Setting the GC threshold from another thread could cause a
        # race between the `gc_should_collect` and `gc_set_threshold` functions.
        NUM_THREADS = 8
        NUM_ITERS = 100_000
        barrier = threading.Barrier(NUM_THREADS)

        class CyclicReference:
            def __init__(self):
                self.r = self

        def allocator():
            barrier.wait()
            for _ in range(NUM_ITERS):
                CyclicReference()

        def setter():
            barrier.wait()
            for i in range(NUM_ITERS):
                gc.set_threshold(100 + (i % 100), 10 + (i % 10), 10 + (i % 10))

        current_threshold = gc.get_threshold()
        try:
            threads = [Thread(target=allocator) for _ in range(NUM_THREADS - 1)]
            threads.append(Thread(target=setter))
            with threading_helper.start_threads(threads):
                pass
        finally:
            gc.set_threshold(*current_threshold)

    def test_get_count(self):
        class CyclicReference:
            def __init__(self):
                self.ref = self

        NUM_ALLOCATORS = 7
        NUM_READERS = 1
        NUM_THREADS = NUM_ALLOCATORS + NUM_READERS
        NUM_ITERS = 1000

        barrier = threading.Barrier(NUM_THREADS)

        def allocator():
            barrier.wait()
            for _ in range(NUM_ITERS):
                CyclicReference()


        def reader():
            barrier.wait()
            for _ in range(NUM_ITERS):
                gc.get_count()

        threads = [Thread(target=allocator) for _ in range(NUM_ALLOCATORS)]
        threads.extend(Thread(target=reader) for _ in range(NUM_READERS))

        with threading_helper.start_threads(threads):
            pass


if __name__ == "__main__":
    unittest.main()
