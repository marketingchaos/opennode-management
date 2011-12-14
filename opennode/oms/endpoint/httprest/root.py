import json
import zope.security.interfaces

from twisted.internet import defer
from twisted.python.failure import Failure
from twisted.web import resource
from twisted.web.server import NOT_DONE_YET
from zope.component import queryAdapter

from opennode.oms.endpoint.httprest.base import IHttpRestView
from opennode.oms.model.traversal import traverse_path
from opennode.oms.security.checker import proxy_factory
from opennode.oms.security.interaction import new_interaction
from opennode.oms.zodb import db


class EmptyResponse(Exception):
    pass


class HttpStatus(Exception):
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


class SeeCanonical(HttpStatus):
    status_code = 301
    status_description = "Moved Permanently"

    def __init__(self, url, *args, **kwargs):
        super(SeeCanonical, self).__init__(*args, **kwargs)
        self.url = url


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

    @db.transact
    def handle_request(self, request):
        """Takes a request, maps it to a domain object and a
        corresponding IHttpRestView, and returns the rendered output
        of that view.

        """
        oms_root = db.get_root()['oms_root']
        objs, unresolved_path = traverse_path(oms_root, request.path[1:])

        if not objs and unresolved_path:
            objs = [oms_root]

        obj = objs[-1]

        view = queryAdapter(obj, IHttpRestView, name=unresolved_path[0] if unresolved_path else '')
        if not view:
            raise NotFound

        interaction = self.get_interaction(request)
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
                if self.get_security_token(request):
                    raise Forbidden()
                raise Unauthorized()

        for method in ('render_' + request.method, 'render'):
            # hasattr will return false on unauthorized fields
            renderer = get_renderer(view, method)
            if renderer:
                return renderer(request)


        raise NotImplemented("method %s not implemented\n" % request.method)

    def get_interaction(self, request):
        # TODO: we can quickly disable rest auth
        # if get_config().getboolean('auth', 'enable_anonymous'):
        #     return None

        token = self.get_security_token(request)

        principal = self.get_principal(token)

        return new_interaction(principal)

    def get_principal(self, token):
        if not token:
            return 'oms.anonymous'
        else:
            # XXX: use real token format
            return token.split('_')[-1]

    def get_security_token(self, request):
        return request.getCookie('oms_auth_token')
