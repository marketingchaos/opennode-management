import logging
import transaction

from zope.authentication.interfaces import IAuthentication
from zope.component import getUtility
from zope.securitypolicy.interfaces import IPrincipalRoleManager

from opennode.oms.model.traversal import traverse1
from opennode.oms.security.interaction import new_interaction
from opennode.oms.security.permissions import Role
from opennode.oms.zodb import db


log = logging.getLogger(__name__)


class NoSuchPermission(Exception):
    pass


@db.ro_transact
def preload_acl_file(iterable, filename=''):
    log.info('Preloading ACL rules...')
    lineno = 0
    try:
        for line in iterable:
            specline = line.split('#', 1)[0]
            if not specline:
                continue
            path, permspec = specline.split(':', 1)
            lineno += 1
            preload_acl_line(path, permspec, filename, lineno)
        transaction.commit()
    except NoSuchPermission as e:
        log.error('No such permission: \'%s\'; file: \'%s\' line: %s', e, filename, lineno)
        log.info('Available permissions: %s', Role.nick_to_role.keys())
        transaction.abort()


def preload_acl_line(path, permspec, filename='-', lineno='-'):
    obj = traverse1(path[1:])

    if obj is None:
        log.warning('No such object: \'%s\'; file: \'%s\' line: %s', path, filename, lineno)
        return

    if obj.__transient__:
        log.warning("Transient object %s always inherits permissions from its parent", path)
        return

    if permspec in ('inherit', 'noinherit'):
        obj.inherit_permissions = (permspec == 'inherit')
        return

    auth = getUtility(IAuthentication, context=None)
    interaction = new_interaction(auth.getPrincipal('root'))
    with interaction:
        prinrole = IPrincipalRoleManager(obj)
        action_map = {'allow': prinrole.assignRoleToPrincipal,
                      'deny': prinrole.removeRoleFromPrincipal,
                      'unset': prinrole.unsetRoleForPrincipal}

        parsedspec = permspec.strip().split(':', 3)
        if len(parsedspec) < 4:
            log.error('Format error: not all fields are specified: \'%s\' on line %s', filename, lineno)
            return

        permtype, kind, principal, perms = parsedspec

        if not perms:
            log.warning('No permissions specified for object: \'%s\'; file: \'%s\' line: %s',
                        path, filename, lineno)
            return

        for perm in perms.strip().split(','):
            if perm not in Role.nick_to_role:
                raise NoSuchPermission(perm)
            role = Role.nick_to_role[perm].id
            log.info('%s \'%s\' on %s (%s) to \'%s\'', permtype, perm, path, obj, principal)
            action_map[permtype](role, principal)
