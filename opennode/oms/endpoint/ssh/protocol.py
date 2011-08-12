import re

from twisted.conch import recvline
from twisted.internet import defer

from opennode.oms.endpoint.ssh import cmd, completion
from opennode.oms.zodb import db


CTRL_A = '\x01'
CTRL_E = '\x05'
CTRL_D = '\x04'
CTRL_K = '\x0b'
CTRL_Y = '\x19'
CTRL_BACKSLASH = '\x1c'
CTRL_L = '\x0c'

class OmsSshProtocol(recvline.HistoricRecvLine):
    """Simple echo protocol.

    Accepts lines of input and writes them back to its connection.  If
    a line consisting solely of "quit" is received, the connection
    is dropped.

    """

    def __init__(self):
        super(OmsSshProtocol, self).__init__()
        self.path = ['']

        @defer.inlineCallbacks
        def _get_obj_path():
            # Here, we simply hope that self.obj_path won't actually be
            # used until it's initialised.  A more fool-proof solution
            # would be to block everything in the protocol while the ZODB
            # query is processing, but that would require a more complex
            # workaround.  This will not be a problem during testing as
            # DB access is blocking when testing.
            self.obj_path = yield db.transact(lambda: [db.ref(db.get_root()['oms_root'])])()

        _get_obj_path()

    def connectionMade(self):
        super(OmsSshProtocol, self).connectionMade()

        self.kill_ring = None
        self.keyHandlers[CTRL_A] = self.handle_HOME
        self.keyHandlers[CTRL_E] = self.handle_END
        self.keyHandlers[CTRL_D] = self.handle_EOF
        self.keyHandlers[CTRL_L] = self.handle_FF
        self.keyHandlers[CTRL_K] = self.handle_KILL_LINE
        self.keyHandlers[CTRL_Y] = self.handle_YANK
        self.keyHandlers[CTRL_BACKSLASH] = self.handle_QUIT

    def lineReceived(self, line):
        line = line.strip()

        if line == 'quit':
            self.terminal.loseConnection()
            return

        cmd_name, cmd_args = line.partition(' ')[::2]
        cmd_handler = cmd.commands().get(cmd_name, None)
        if cmd_handler:
            cmd_args = cmd_args.strip()
            if cmd_args:
                cmd_args = re.split(r'\s+', cmd_args)
            else:
                cmd_args = []
            deferred = defer.maybeDeferred(cmd_handler(self), *cmd_args)
        else:
            if line:
                self.terminal.write('No such command: %s' % cmd_name)
                self.terminal.nextLine()
            deferred = defer.Deferred()
            deferred.callback(None)

        @deferred
        def on_success(ret):
            self.terminal.write(self.ps[self.pn])

        @deferred
        def on_error(f):
            f.raiseException()
            self.terminal.nextLine()
            self.terminal.write(self.ps[self.pn])

        ret = defer.Deferred()
        deferred.addBoth(ret.callback)
        return ret

    @db.transact
    def handle_TAB(self):
        patch = completion.complete(self, self.lineBuffer, self.lineBufferIndex)
        if patch:
            self.terminal.write(patch)
            lead, rest = self.lineBuffer[0:self.lineBufferIndex], self.lineBuffer[self.lineBufferIndex:]
            self.lineBuffer = lead + list(patch) + rest
            self.lineBufferIndex += len(patch)

    def handle_EOF(self):
        """Exit the shell on CTRL-D"""
        if self.lineBuffer:
            self.terminal.write('\a')
        else:
            self.handle_QUIT()

    def handle_FF(self):
        """Handle a 'form feed' byte - generally used to request a screen
        refresh/redraw.
        """
        self.terminal.eraseDisplay()
        self.terminal.cursorHome()
        self.drawInputLine()

    def handle_KILL_LINE(self):
        """Deletes the rest of the line (from the cursor right), and keeps the content
        in the kill ring for future pastes
        """
        self.terminal.eraseToLineEnd()
        self.kill_ring = self.lineBuffer[self.lineBufferIndex:]
        self.lineBuffer = self.lineBuffer[0:self.lineBufferIndex]

    def handle_YANK(self):
        """Paste the content of the kill ring"""
        if self.kill_ring:
            self.terminal.write("".join(self.kill_ring))
            lead, rest = self.lineBuffer[0:self.lineBufferIndex], self.lineBuffer[self.lineBufferIndex:]
            self.lineBuffer = lead + self.kill_ring + rest
            self.lineBufferIndex += len(self.kill_ring)

    def handle_QUIT(self):
        """Just copied from conch Manhole, no idea why it would be useful to differentiate it from CTRL-D,
        but I guess it's here for a reason"""
        self.terminal.loseConnection()

    @property
    def ps(self):
        ps1 = '%s@%s:%s%s ' % ('user', 'oms', self._cwd(), '#')
        return [ps1, '... ']

    def _cwd(self):
        return self.make_path(self.path)

    @staticmethod
    def make_path(path):
        return '/'.join(path) or '/'
