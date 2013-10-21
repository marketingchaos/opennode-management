import functools
import json
import zope.security.interfaces

from twisted.internet import defer
from twisted.python import log, failure
from twisted.web import resource
from twisted.web.server import NOT_DONE_YET
from twisted.python.compat import intToBytes

from zope.component import queryAdapter, getUtility

from opennode.oms.config import get_config
from opennode.oms.endpoint.httprest.base import IHttpRestView, IHttpRestSubViewFactory
from opennode.oms.model.traversal import traverse_path
from opennode.oms.security.checker import proxy_factory
from opennode.oms.security.interaction import new_interaction
from opennode.oms.util import JsonSetEncoder
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

    headers = {'WWW-Authenticate': 'Basic realm=OMS'}


class Forbidden(HttpStatus):
    status_code = 403
    status_description = "Forbidden"


class BadRequest(HttpStatus):
    status_code = 400
    status_description = "Bad Request"


class MethodNotAllowed(HttpStatus):
    status_code = 405
    status_description = "Method not allowed"

    def __init__(self, msg, allow):
        HttpStatus.__init__(self, msg)
        self.headers = {'Allow': ','.join(allow)}


def log_wrapper(self, f, server):
    @functools.wraps(f)
    def log_(request):
        """
        Log a request's result to the logfile, by default in combined log format.
        """
        if hasattr(request, 'interaction'):
            principals = map(lambda pp: pp.principal.id, request.interaction.participations)
        else:
            principals = []
        if hasattr(self, "logFile"):
            line = '%s %s - %s "%s" %d %s "%s" "%s"\n' % (
                request.getClientIP(),
                principals,
                self._logDateTime,
                '%s %s %s' % (self._escape(request.method),
                              self._escape(request.uri),
                              self._escape(request.clientproto)),
                request.code,
                request.sentLength or "-",
                self._escape(request.getHeader("referer") or "-"),
                self._escape(request.getHeader("user-agent") or "-"))
            self.logFile.write(line)
    return log_


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
        self.use_keystone_tokens = get_config().getboolean('auth', 'use_keystone', False)

    def render(self, request):
        request.site.log = log_wrapper(request.site, request.site.log, self)
        deferred = self._render(request)

        @deferred
        def on_error(error):
            log.err(error, system='httprest')

        return NOT_DONE_YET

    @defer.inlineCallbacks
    def _render(self, request):
        origin = request.getHeader('Origin')
        if origin:
            request.setHeader('Access-Control-Allow-Origin', origin)
            request.setHeader('Access-Control-Allow-Credentials', 'true')
        else:
            request.setHeader('Access-Control-Allow-Origin', '*')
        request.setHeader('Access-Control-Allow-Methods', 'GET, PUT, POST, DELETE, OPTIONS, HEAD')
        request.setHeader('Access-Control-Allow-Headers',
                          'Origin, Content-Type, Cache-Control, X-Requested-With')

        ret = None
        try:
            ret = yield self.handle_request(request)
            # allow views to take full control of output streaming
            if ret is not NOT_DONE_YET and ret is not EmptyResponse:
                request.setHeader('Content-Type', 'application/json')
                json_data = json.dumps(ret, indent=2, cls=JsonSetEncoder)
                request.setHeader('Content-Length', intToBytes(len(json_data)))
                request.write(json_data)
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
            request.setHeader('Content-Type', 'text/plain')
            request.setResponseCode(500, "Server Error")
            error_message = "%s %s\n\n" % (500, "Server Error")
            request.setHeader('Content-Length', intToBytes(len(error_message)))
            request.write(error_message)
            log.err(system='httprest')
            failure.Failure().printTraceback(request)
        finally:
            if ret is not NOT_DONE_YET:
                request.finish()

    def check_auth(self, request):
        from opennode.oms.endpoint.httprest.auth import IHttpRestAuthenticationUtility, ISessionStorage

        authenticator = getUtility(IHttpRestAuthenticationUtility)
        session = authenticator.get_twisted_session(request)
        if session:
            return ISessionStorage(session).username

    def find_view(self, obj, unresolved_path, request):

        if len(unresolved_path) == 0:
            view = queryAdapter(obj, IHttpRestView)
            return view
        else:
            view = queryAdapter(obj, IHttpRestView, name=unresolved_path[0])
            if view:
                return view

        subview_factory = queryAdapter(obj, IHttpRestSubViewFactory)

        subview = subview_factory.resolve(unresolved_path, request) if subview_factory else None

        if not subview:
            raise NotFound
        return subview

    @db.transact
    def handle_request(self, request):
        """Takes a request, maps it to a domain object and a corresponding IHttpRestView
        and returns the rendered output of that view.
        """
        principal = self.check_auth(request)

        oms_root = db.get_root()['oms_root']
        objs, unresolved_path = traverse_path(oms_root, request.path[1:])

        if not objs and unresolved_path:
            objs = [oms_root]

        obj = objs[-1]

        interaction = self.get_interaction(request, principal)
        request.interaction = interaction

        if self.use_security_proxy:
            obj = proxy_factory(obj, interaction)

        view = self.find_view(obj, unresolved_path, request)

        needs_rw_transaction = view.rw_transaction(request)

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
                raise Forbidden('User does not have permission to access this resource')

        for method in ('render_' + request.method,
                       'render_' + request.method.lower(),
                       'render'):
            renderer = get_renderer(view, method)
            if renderer:
                from opennode.oms.endpoint.httprest.auth import AuthView
                if isinstance(view, AuthView) and renderer.__name__ == 'render':
                    res = renderer(request, self.use_keystone_tokens)
                else:
                    res = renderer(request)
                return res if needs_rw_transaction else db.RollbackValue(res)

        raise NotImplementedError("Method %s is not implemented in %s\n" % (request.method, view))

    def get_interaction(self, request, principal='oms.anonymous'):
        # TODO: we can quickly disable rest auth
        # if get_config().getboolean('auth', 'enable_anonymous'):
        #     return None
        if request.method == 'OPTIONS':
            principal = 'oms.rest_options'
        return new_interaction(principal)
