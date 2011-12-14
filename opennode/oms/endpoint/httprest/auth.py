import json

from grokcore.component import context, name
from twisted.internet import defer
from twisted.cred.checkers import InMemoryUsernamePasswordDatabaseDontUse
from twisted.cred.credentials import UsernamePassword
from twisted.cred.error import UnauthorizedLogin
from twisted.web.guard import BasicCredentialFactory

from opennode.oms.model.model.root import OmsRoot
from opennode.oms.endpoint.httprest.base import HttpRestView
from opennode.oms.endpoint.httprest.root import BadRequest

from twisted.web.server import NOT_DONE_YET


class AuthView(HttpRestView):
    context(OmsRoot)
    name('auth')

    checkers = [InMemoryUsernamePasswordDatabaseDontUse(user="supersecret")]

    realm = 'OMS'

    # Should be render_GET but ONC (i.e. ExtJS) cannot attach a request body to GET requests
    def render(self, request):
        body = request.content.getvalue()
        credentials = None

        if body:
            try:
                params = json.loads(body)
            except ValueError:
                raise BadRequest, "The request body not JSON-parsable"

            # cannot be unicode
            username = str(params['username'])
            password = str(params['password'])

            credentials = UsernamePassword(username, password)
        else:
            basic_auth = request.requestHeaders.getRawHeaders('Authorization', [None])[0]
            if basic_auth:
                bc = BasicCredentialFactory(self.realm)
                try:
                    credentials = bc.decode(basic_auth.split(' ')[1], None)
                except:
                    raise BadRequest, "The Authorization header was not parsable"

        @defer.inlineCallbacks
        def authenticate():
            avatar = None
            for i in self.checkers:
                try:
                    avatar = yield i.requestAvatarId(credentials)
                    break
                except UnauthorizedLogin:
                    continue

            if avatar:
                token = self.generate_token(credentials)
                request.addCookie('oms_auth_token', token)
                request.write(json.dumps({'status': 'success', 'token': token}))
            else:
                request.setResponseCode(401)
                request.responseHeaders.addRawHeader('WWW-Authenticate', 'Basic realm="%s"' % self.realm)
                request.write(json.dumps({'status': 'failure'}))

            request.finish()

        authenticate()
        return NOT_DONE_YET

    def generate_token(self, credentials):
        # XXX: todo real cryptographic token
        return 'fake_token_%s' % credentials.username


class LogoutView(HttpRestView):
    context(OmsRoot)
    name('logout')

    checkers = [InMemoryUsernamePasswordDatabaseDontUse(user="supersecret")]

    realm = 'OMS'

    def render_GET(self, request):
        request.addCookie('oms_auth_token', '', expires='Wed, 01 Jan 2000 00:00:00 GMT')
        return {'status': 'success'}
