import zope.interface
from zope.component import getSiteManager, implementedBy
from zope.interface import classImplements


def get_direct_interfaces(obj):
    """Returns the interfaces that the parent class of `obj`
    implements, exluding any that any of its ancestor classes
    implement.

    >>> from zope.interface import Interface, implements, implementedBy
    >>> class IA(Interface): pass
    >>> class IB(Interface): pass
    >>> class A: implements(IA)
    >>> class B(A): implements(IB)
    >>> b = B()
    >>> [i.__name__ for i in list(implementedBy(B).interfaces())]
    ['IB', 'IA']
    >>> [i.__name__ for i in get_direct_interfaces(b)]
    ['IB']

    """
    cls = obj if isinstance(obj, type) else type(obj)

    if not isinstance(obj, type) and hasattr(obj, 'implemented_interfaces') :
        interfaces = obj.implemented_interfaces()
    else:
        interfaces = list(zope.interface.implementedBy(cls).interfaces())

    for base_cls in cls.__bases__:
        for interface in list(zope.interface.implementedBy(base_cls).interfaces()):
            # in multiple inheritance this it could be already removed
            if interface in interfaces:
                interfaces.remove(interface)

    return interfaces


def get_direct_interface(obj):
    interfaces = get_direct_interfaces(obj)
    if not interfaces:
        return None
    if len(interfaces) == 1:
        return interfaces[0]
    else:
        raise Exception("Object implements more than 1 interface")


def query_adapter_for_class(cls, interface):
    return getSiteManager().adapters.lookup([implementedBy(cls)], interface)


class Singleton(type):
    """Singleton metaclass."""

    def __init__(cls, name, bases, dict):
        super(Singleton, cls).__init__(name, bases, dict)
        cls.instance = None

    def __call__(cls,*args,**kw):
        if cls.instance is None:
            cls.instance = super(Singleton, cls).__call__(*args, **kw)
        return cls.instance


def subscription_factory(cls, *args, **kwargs):
    """Utility which allows to to quickly register a subscription adapters which returns new instantiated objects
    of a given class

    >>> provideSubscriptionAdapter(subscription_factory(MetricsDaemonProcess), adapts=(IProc,))

    """

    class SubscriptionFactoryWrapper(object):
        def __new__(self, *_ignore):
            return cls(*args)

    interfaces = get_direct_interfaces(cls)
    classImplements(SubscriptionFactoryWrapper, *interfaces)
    return SubscriptionFactoryWrapper


def adapter_value(value):
    """Utility which allows to to quickly register a subscription adapter  as a value instead of

    >>> provideSubscriptionAdapter(adapter_value(['useful', 'stuff']), adapts=(Compute,), provides=ISomething)

    """

    def wrapper(*_):
        return value
    return wrapper


def blocking_yield(deferred):
    """This utility is part of the HDK (hack development toolkit) use with care and remove it's usage asap.

    Sometimes we have to synchronously wait for a deferred to complete,
    for example when executing inside db.transact code, which cannot 'yield'
    because currently db.transact doesn't handle returning a deferred.

    Or because we are running code inside a handler which cannot return a deferred
    otherwise we cannot block the caller or rollback the transaction in case of async code
    throwing exception (scenario: we want to prevent deletion of node)

    Use this utility only until you refactor the upstream code in order to use pure async code.
    """

    import time
    from twisted.python.failure import Failure

    while not deferred.called:
        time.sleep(0.1)

    res = deferred.result
    if isinstance(res, Failure):
        raise res.value
