import functools
import inspect
import logging
import pickle
import random
import struct
import subprocess
import threading
import time
import transaction

from ZEO.ClientStorage import ClientStorage
from ZODB.FileStorage import FileStorage
from ZODB.POSException import ConflictError, ReadConflictError, StorageTransactionError
from grokcore.component import subscribe
from twisted.internet import reactor, defer
from twisted.internet.threads import deferToThreadPool
from twisted.python.threadable import isInIOThread
from twisted.python.threadpool import ThreadPool
from zope.component import handle
from zope.interface import Interface, implements

from opennode.oms.config import get_config
from opennode.oms.core import IBeforeApplicationInitializedEvent
from opennode.oms.model.model import OmsRoot
from opennode.oms.zodb.proxy import (make_persistent_proxy,
                                     remove_persistent_proxy as _remove_persistent_proxy,
                                     get_peristent_context, PersistentProxy)
from opennode.oms.zodb.extractors import context_from_method


__all__ = ['get_db', 'get_connection', 'get_root', 'transact', 'ref', 'deref']


_db = None
_threadpool = None
_connection = threading.local()
_testing = False
_context = threading.local()


log = logging.getLogger(__name__)


class RollbackException(Exception):
    """Raised to cause a clean rollback of the transaction"""


class RollbackValue(object):
    def __init__(self, value):
        self.value = value


class IBeforeDatabaseInitializedEvent(Interface):
    """Emitted before database is initialized"""


class BeforeDatabaseInitalizedEvent(object):
    implements(IBeforeDatabaseInitializedEvent)


def init_threadpool():
    global _threadpool

    _threadpool = ThreadPool(minthreads=0, maxthreads=20)

    reactor.callWhenRunning(_threadpool.start)
    reactor.addSystemEventTrigger('during', 'shutdown', _threadpool.stop)


def get_db_dir():
    db_dir = 'db'
    try:
        # useful during development
        db_dir = subprocess.check_output('scripts/current_db_dir.sh').strip()
    except:
        pass

    if db_dir == 'db':
        db_dir = get_config().get('db', 'path')

    return db_dir


@subscribe(IBeforeApplicationInitializedEvent)
def initialize_database(event):
    init()


def init(test=False):
    global _db, _testing

    if _db and not test:
        return

    log.info("Initializing zodb")
    handle(BeforeDatabaseInitalizedEvent())

    if not test:
        storage_type = get_config().get('db', 'storage_type')

        if storage_type == 'zeo':
            from ZODB import DB
            storage = ClientStorage('%s/socket' % get_db_dir())
            _db = DB(storage)
        elif storage_type == 'embedded':
            from ZODB import DB
            storage = FileStorage('%s/data.fs' % get_db_dir())
            _db = DB(storage)
        elif storage_type == 'memory':
            from ZODB.tests.util import DB
            _db = DB()
        else:
            raise Exception("Unknown storage type '%s'" % storage_type)
    else:
        from ZODB.tests.util import DB
        _db = DB()
        _testing = True

    init_schema()


def init_schema():
    root = get_root(True)

    if 'oms_root' not in root:
        root['oms_root'] = OmsRoot()
        transaction.commit()


def get_db():
    if not _db:
        raise Exception('DB not initalized')
    if isInIOThread() and not _testing:
        raise Exception('The ZODB should not be accessed from the main thread')
    return _db


def get_connection(accept_main_thread=False):
    if not accept_main_thread and isInIOThread() and not _testing:
        raise Exception('The ZODB should not be accessed from the main thread')

    global _connection
    if not hasattr(_connection, 'x'):
        _connection.x = get_db().open()
    return _connection.x


def get_root(accept_main_thread=False):
    return get_connection(accept_main_thread).root()


def assert_transact(fun):
    """Used to decorate methods which assume to be running in a threadpool used for blocking io,
    for example those created by @db.transact.

    """

    @functools.wraps(fun)
    def wrapper(*args, **kwargs):
        if isInIOThread() and not _testing:
            raise Exception('The ZODB should not be accessed from the main thread')
        return fun(*args, **kwargs)
    return wrapper


def transact(fun):
    """Runs a callable inside a separate thread within a ZODB transaction.

    Returned values are deeply copied. Currently only zodb objects returned directly or
    contained in the first level content of lists/sets/dicts are copied.
    """
    if not _threadpool:
        init_threadpool()

    @functools.wraps(fun)
    def run_in_tx(fun, *args, **kwargs):
        if not _db:
            raise Exception('DB not initalized')

        context = context_from_method(fun, args, kwargs)
        _context.x = context

        cfg = get_config()

        def trace(msg, t, force=False):
            ch = '/'
            if msg == "BEGINNING":
                ch = '\\'
            if cfg.getboolean('debug', 'trace_transactions', False) or force:
                trace_fun = log.error
            else:
                trace_fun = log.debug
            trace_fun("%s\ttx:%s %s\tin %s from %s, line %s %s" %
                      (msg, t.description, ch, fun, fun.__module__, inspect.getsourcelines(fun)[1], ch))

        retries = cfg.getint('db', 'conflict_retries')

        retrying = False
        for i in xrange(0, retries + 1):
            try:
                t = transaction.begin()
                t.note("%s" % (random.randint(0, 1000000)))
                trace("BEGIN", t)
                result = fun(*args, **kwargs)
            except RollbackException:
                transaction.abort()
                return
            except:
                trace("ROLLBACK ON ERROR", t)
                transaction.abort()
                raise
            else:
                try:
                    if isinstance(result, RollbackValue):
                        trace("ROLLBACK", t)
                        result = result.value
                        transaction.abort()
                    else:
                        trace("COMMIT", t)
                        transaction.commit()
                        if retrying:
                            trace("Succeeded commit, after %s attempts" % i, t)

                    _context.x = None
                    return make_persistent_proxy(result, context)
                except ReadConflictError as e:
                    trace("GOT READ CONFLICT IN RW TRANSACT, retrying %s" % i, t, force=True)
                    retrying = True
                    time.sleep(random.random() * 0.2)
                except ConflictError as e:
                    trace("GOT WRITE CONFLICT IN RW TRANSACT, retrying %s" % i, t, force=True)
                    retrying = True
                    time.sleep(random.random() * 0.2)
                except StorageTransactionError as e:
                    if e.args and e.args[0] == "Duplicate tpc_begin calls for same transaction":
                        # This may happen when an object attached to one connection is used in anther
                        # connection's transaction. Check and compare _p_jar attributes of all objects
                        # involved in this transaction! They all must be the same.
                        trace("DUPLICATE tpc_begin IN RW TRANSACT", t, force=True)
                    raise
                except:
                    trace('ABORT: bad commit attempt', t)
                    transaction.abort()
                    raise
        raise e

    @functools.wraps(fun)
    def wrapper(*args, **kwargs):
        if not _testing:
            return deferToThreadPool(reactor, _threadpool,
                                     run_in_tx, fun, *args, **kwargs)
        else:
            # No threading during testing
            return defer.execute(run_in_tx, fun, *args, **kwargs)
    return wrapper


def ro_transact(fun=None, proxy=True):
    if fun is None:
        def wrapper(fun):
            return _ro_transact(fun, proxy)
        return wrapper
    return _ro_transact(fun, proxy)


def _ro_transact(fun, proxy=True):
    """Runs a callable inside a separate thread within a readonly ZODB transaction.

    Transaction is always rolledback.

    Returned values are deeply copied. Currently only zodb objects returned directly or
    contained in the first level content of lists/sets/dicts are copied.

    """

    if not _threadpool:
        init_threadpool()

    @functools.wraps(fun)
    def run_in_tx(fun, *args, **kwargs):
        context = context_from_method(fun, args, kwargs)
        _context.x = context

        if not _db:
            raise Exception('DB not initalized')

        try:
            transaction.begin()
            _context.x = None

            res = fun(*args, **kwargs)
            if proxy:
                return make_persistent_proxy(res, context)
            return res
        finally:
            transaction.abort()

    @functools.wraps(fun)
    def wrapper(*args, **kwargs):
        if not _testing:
            return deferToThreadPool(reactor, _threadpool,
                                     run_in_tx, fun, *args, **kwargs)
        else:
            return defer.execute(run_in_tx, fun, *args, **kwargs)
    return wrapper


def data_integrity_validator(fun):
    """Runs a callable inside all available threads in the threadpool within a readonly ZODB transaction.

    Calls function that is expected to assert some expectations about DB data and throw an exception if
    anything is wrong.

    Usage:

        >>> @data_integrity_validator
        ... def assert_some_value(obj, field, expected):
        ....    assert getattr(obj, field) == expected

    This will run the assertion on all open database connections and assert that all values are as expected

    Transaction is always rolled back.
    """

    if not _threadpool:
        init_threadpool()

    _done_threads = set()
    _all_done = threading.Event()

    @functools.wraps(fun)
    def run_in_tx(fun, *args, **kwargs):
        context = context_from_method(fun, args, kwargs)
        _context.x = context

        if not _db:
            raise Exception('DB not initalized')

        thread = threading.currentThread()
        log.debug('integrity: validating %s: %s', thread, fun)

        try:
            transaction.begin()
            _context.x = None

            if thread in _done_threads:
                return

            fun(*args, **kwargs)
        except:
            log.error('integrity: FAILED! %s (%s)', thread, fun, exc_info=True)
        else:
            log.debug('integrity: PASSED! %s (%s)', thread, fun)
        finally:
            transaction.abort()
            _done_threads.add(thread)
            _all_done.wait()

    @functools.wraps(fun)
    def wrapper(*args, **kwargs):
        log.debug('integrity: started: %s', fun)

        if not _testing:
            deferred_list = []

            if len(_threadpool.working) > 0:
                log.info('integrity: There are working threads while testing %s: %s',
                         fun, _threadpool.working)

            for thread in _threadpool.waiters:
                if thread in _done_threads:
                    continue
                d = deferToThreadPool(reactor, _threadpool, run_in_tx, fun, *args, **kwargs)
                deferred_list.append(d)

            dl = defer.DeferredList(deferred_list)
            _all_done.set()
            return dl
    return wrapper



def ref(obj):
    return obj._p_oid


def deref(obj_id):
    assert isinstance(obj_id, str)
    return get_connection().get(obj_id)


def load_object(oid, tid):
    if tid:
        oid_s = struct.pack('!q', int(oid, 0))
        tid_s = struct.pack('!q', int(tid, 0))
        return pickle.loads(get_db().storage.loadSerial(oid_s, tid_s))
    else:
        return deref(oid)


def get(obj, name):
    return ro_transact(lambda: getattr(obj, name))()


def context(obj):
    if getattr(_context, 'x', None):
        return _context.x
    return get_peristent_context(obj)


def assert_proxy(obj):
    if any((not (isinstance(obj, PersistentProxy)), isinstance(obj, basestring), isinstance(obj, int),
            isinstance(obj, float), isinstance(obj, defer.Deferred))):
        log.error("Should be a db proxy %s %s" % (type(obj), obj))
        import traceback
        traceback.print_stack()
    assert any((isinstance(obj, PersistentProxy),
               isinstance(obj, basestring),
               isinstance(obj, int),
               isinstance(obj, float),
               isinstance(obj, defer.Deferred)))


def assert_not_proxy(obj):
    if any(isinstance(obj, PersistentProxy), isinstance(obj, basestring), isinstance(obj, int),
           isinstance(obj, float), isinstance(obj, defer.Deferred)):
        log.error("Should be a db proxy %s %s" % (type(obj), obj))
        import traceback
        traceback.print_stack()
    assert any(not (isinstance(obj, PersistentProxy), isinstance(obj, basestring), isinstance(obj, int),
                    isinstance(obj, float), isinstance(obj, defer.Deferred)))


remove_persistent_proxy = _remove_persistent_proxy
