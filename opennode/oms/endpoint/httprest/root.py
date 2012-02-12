import json
import zope.security.interfaces

from functools import wraps
from twisted.internet import defer
from twisted.python.failure import Failure
from twisted.web import resource
from twisted.web.server import NOT_DONE_YET
from zope.component import queryAdapter, getUtility

from opennode.oms.config import get_config
from opennode.oms.endpoint.httprest.base import IHttpRestView, IHttpRestSubViewFactory
from opennode.oms.model.traversal import traverse_path
from opennode.oms.security.checker import proxy_factory
from opennode.oms.security.interaction import new_interaction
from opennode.oms.util import blocking_yield
from opennode.oms.zodb import db


class EmptyResponse(Exception):
    pass


class HttpStatus(Exception):
    def __init__(self, body=None, *args, **kwargs):
        super(HttpStatus, self).__init__(*args, **kwargs)
        self.body = body

    @property
    def status_code(self):
        raise NotImplementedError

    @property
    def status_description(self):
        raise NotImplementedError

    headers = {}


class NotFound(HttpStatus):
    status_code = 404
    status_description = "Not Found"


class NotImplemented(HttpStatus):
    status_code = 501
    status_description = "Not Implemented"


class AbstractRedirect(HttpStatus):
    def __init__(self, url, *args, **kwargs):
        super(AbstractRedirect, self).__init__(*args, **kwargs)
        self.url = url

    @property
    def headers(self):
        return {'Location': self.url}


class SeeCanonical(AbstractRedirect):
    status_code = 301
    status_description = "Moved Permanently"


class SeeOther(AbstractRedirect):
    status_code = 303
    status_description = "Moved Temporarily"


class Unauthorized(HttpStatus):
    status_code = 401
    status_description = "Authorization Required"

    headers = {'WWW-Authenticate': 'Basic realm=OMS',
               'Set-Cookie': 'oms_auth_token=;expires=Wed, 01 Jan 2000 00:00:00 GMT'}


class Forbidden(HttpStatus):
    status_code = 403
    status_description = "Forbidden"


class BadRequest(HttpStatus):
    status_code = 400
    status_description = "Bad Request"


def adaptive_transact(fun):
    """Wrap in RO transact or RW transaction, depending on the http request method"""
    @wraps(fun)
    def wrapper(self, request):
        if request.method == 'GET':
            return db.ro_transact(fun)(self, request)
        return db.transact(fun)(self, request)
    return wrapper


class HttpRestServer(resource.Resource):
    """Restful HTTP API interface for OMS.

    Exposes a JSON web service to communicate with OMS.

    """

    def getChild(self, name, request):
        """We are the handler for anything below this base url, except what explicitly added in oms.tac."""
        return self

    def __init__(self, avatar=None):
        ## Twisted Resource is a not a new style class, so emulating a super-call
        resource.Resource.__init__(self)
        self.avatar = avatar

        self.use_security_proxy = get_config().getboolean('auth', 'security_proxy_rest')

    def render(self, request):
        self._render(request)
        return NOT_DONE_YET

    @defer.inlineCallbacks
    def _render(self, request):
        request.setHeader('Content-type', 'application/json')
        origin = request.getHeader('Origin')
        if origin:
            request.setHeader('Access-Control-Allow-Origin', origin)
            request.setHeader('Access-Control-Allow-Credentials', 'true')
        else:
            request.setHeader('Access-Control-Allow-Origin', '*')
        request.setHeader('Access-Control-Allow-Methods', 'GET, PUT, POST, DELETE, OPTIONS, HEAD')
        request.setHeader('Access-Control-Allow-Headers', 'Origin, Content-Type, Cache-Control, X-Requested-With')

        ret = None
        try:
            ret = yield self.handle_request(request)
            if ret is EmptyResponse:
                raise ret
        except EmptyResponse:
            pass
        except HttpStatus as exc:
            request.setResponseCode(exc.status_code, exc.status_description)
            for name, value in exc.headers.items():
                request.responseHeaders.addRawHeader(name, value)
            if exc.body:
                request.write(json.dumps(exc.body))
            else:
                request.write("%s %s\n" % (exc.status_code, exc.status_description))
            if exc.message:
                request.write("%s\n" % exc.message)
        except Exception:
            request.setResponseCode(500, "Server Error")
            request.write("%s %s\n\n" % (500, "Server Error"))
            # TODO: if DEBUG:
            Failure().printTraceback(request)
        else:
            # allow views to take full control of output streaming
            if ret != NOT_DONE_YET:
                def render(obj):
                    print "RENDERING ERROR, cannot json serialize", obj
                    raise TypeError

                request.write(json.dumps(ret, indent=2, default=render) + '\n')
        finally:
            if ret != NOT_DONE_YET:
                request.finish()

    def check_auth(self, request):
        from opennode.oms.endpoint.httprest.auth import IHttpRestAuthenticationUtility

        authentication_utility = getUtility(IHttpRestAuthenticationUtility)
        credentials = authentication_utility.get_basic_auth_credentials(request)
        if credentials:
            blocking_yield(authentication_utility.authenticate(request, credentials, basic_auth=True))
            return authentication_utility.generate_token(credentials)
        else:
            return authentication_utility.get_token(request)

    @adaptive_transact
    def handle_request(self, request):
        """Takes a request, maps it to a domain object and a
        corresponding IHttpRestView, and returns the rendered output
        of that view.

        """

        token = self.check_auth(request)

        oms_root = db.get_root()['oms_root']
        objs, unresolved_path = traverse_path(oms_root, request.path[1:])

        if not objs and unresolved_path:
            objs = [oms_root]

        obj = objs[-1]

        interaction = self.get_interaction(request, token)
        request.interaction = interaction

        if self.use_security_proxy:
            obj = proxy_factory(obj, interaction)

        view = queryAdapter(obj, IHttpRestView, name=unresolved_path[0] if unresolved_path else '')

        sub_view_factory = queryAdapter(view, IHttpRestSubViewFactory)
        if sub_view_factory:
            view = sub_view_factory.resolve(unresolved_path[1:])

        if not view:
            raise NotFound

        # create a security proxy if we have a secured interaction
        if interaction:
            try:
                view = proxy_factory(view, interaction)
            except:
                # XXX: TODO: define a real exception for this proxy creation error
                # right now we want to ignore security when there are no declared rules
                # on how to secure a view
                pass

        def get_renderer(view, method):
            try:
                return getattr(view, method, None)
            except zope.security.interfaces.Unauthorized:
                from opennode.oms.endpoint.httprest.auth import IHttpRestAuthenticationUtility

                if token or not getUtility(IHttpRestAuthenticationUtility).get_basic_auth_credentials(request):
                    raise Forbidden()
                raise Unauthorized()

        for method in ('render_' + request.method, 'render'):
            # hasattr will return false on unauthorized fields
            renderer = get_renderer(view, method)
            if renderer:
                return renderer(request)

        raise NotImplemented("method %s not implemented\n" % request.method)

    def get_interaction(self, request, token):
        # TODO: we can quickly disable rest auth
        # if get_config().getboolean('auth', 'enable_anonymous'):
        #     return None

        from opennode.oms.endpoint.httprest.auth import IHttpRestAuthenticationUtility

        authentication_utility = getUtility(IHttpRestAuthenticationUtility)
        try:
            principal = authentication_utility.get_principal(token)
        except:
            # Avoid that changes in format of security token will require every user
            # to flush the cookies
            principal = 'oms.anonymous'
        if principal != 'oms.anonymous':
            authentication_utility.renew_token(request, token)

        return new_interaction(principal)
