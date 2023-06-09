# -*- coding: utf-8 -*-
#
# Copyright (C) 2005-2007 Matthew Good <trac@matt-good.net>
# Copyright (C) 2011,2012 Steffen Hoffmann <hoff.st@web.de>
# All rights reserved.
#
# This software is licensed as described in the file COPYING, which
# you should have received as part of this distribution.
#
# Author: Matthew Good <trac@matt-good.net>

import errno
import io
import os

from trac.config import Option
from trac.core import Component, TracError, implements

from .api import IPasswordStore, _, N_
from .compat import compare_digest, unicode
from .pwhash import check_hash, generate_hash, htdigest
from .util import EnvRelativePathOption


def _eachline(filename, encoding='utf-8'):
    with io.open(filename, 'r', encoding=encoding) as f:
        for line in f:
            yield line


def _writelines(filename, lines, encoding='utf-8'):
    with io.open(filename, 'w', encoding=encoding) as f:
        for line in lines:
            if isinstance(line, bytes):
                line = unicode(line, 'utf-8')
            f.write(line)


class AbstractPasswordFileStore(Component):
    """Base class for managing password files.

    Derived classes support different formats such as
    Apache's htpasswd and htdigest format.
    See these concrete sub-classes for usage information.
    """
    abstract = True

    # Note: 'filename' is a required, store-specific option.

    def has_user(self, user):
        return user in self.get_users()

    def get_users(self):
        filename = self.filename
        if not os.path.exists(filename):
            self.log.error('acct_mgr: get_users() -- '
                           'Can\'t locate password file "%s"', filename)
            return []
        return self._get_users(filename)

    def set_password(self, user, password, old_password=None, overwrite=True):
        return not self._update_file(self.prefix(user),
                                     self.userline(user, password),
                                     overwrite)

    def delete_user(self, user):
        return self._update_file(self.prefix(user), None)

    def check_password(self, user, password):
        filename = self.filename
        if not os.path.exists(filename):
            self.log.error('acct_mgr: check_password() -- '
                           'Can\'t locate password file "%s"', filename)
            return False
        prefix = self.prefix(user)
        try:
            for line in _eachline(filename):
                if line.startswith(prefix):
                    line = line[len(prefix):].rstrip('\n')
                    return self._check_userline(user, password, line)
        except (OSError, IOError):
            self.log.error('acct_mgr: check_password() -- '
                           'Can\'t read password file "%s"', filename)
        return None

    def _update_file(self, prefix, userline, overwrite=True):
        """Add or remove user and change password.

        If `userline` is empty, the line starting with `prefix` is removed
        from the user file. Otherwise the line starting with `prefix`
        is updated to `userline`.  If no line starts with `prefix`,
        the `userline` is appended to the file.

        Returns `True` if a line matching `prefix` was updated,
        `False` otherwise.
        """
        if not self.filename:
            option = self.__class__.filename
            raise TracError(
                _("[%(section)s] %(name)s option for the password "
                  "file is not configured",
                  section=option.section, name=option.name))
        filename = self.filename
        matched = False
        new_lines = []
        eol = '\n'
        try:
            # Open existing file read-only to read old content.
            lines = list(_eachline(filename))
        except EnvironmentError as e:
            if e.errno == errno.ENOENT:
                # Ignore, when file doesn't exist and create it below.
                pass
            elif e.errno == errno.EACCES:
                raise TracError(_(
                    "The password file could not be read. Trac requires read "
                    "and write access to both the password file and its "
                    "parent directory."))
            else:
                raise
        else:
            # DEVEL: Beware, in shared use there is a race-condition,
            #   since file changes by other programs that occur from now on
            #   are currently not detected and will get overwritten.
            #   This could be fixed by file locking, but a cross-platform
            #   implementation is certainly non-trivial.
            if len(lines) > 0:
                # predict eol style for lines without eol characters
                if not os.linesep == '\n':
                    if lines[-1].endswith('\r') and os.linesep == '\r':
                        # antique MacOS newline style safeguard
                        # DEVEL: is this really still needed?
                        eol = '\r'
                    elif lines[-1].endswith('\r\n') and os.linesep == '\r\n':
                        # Windows newline style safeguard
                        eol = '\r\n'

                for line in lines:
                    if line.startswith(prefix):
                        if not matched and userline:
                            if overwrite:
                                new_lines.append(userline + eol)
                            else:
                                new_lines.append(line.rstrip('\r\n') + eol)
                        matched = True
                    # preserve existing lines with proper eol
                    elif line.endswith(eol) and not \
                            (eol == '\n' and line.endswith('\r\n')):
                        new_lines.append(line)
                    # unify eol style using confirmed default and
                    # make sure the (last) line has a newline anyway
                    else:
                        new_lines.append(line.rstrip('\r\n') + eol)

        # Finally add the new line here, if it wasn't used before
        # to update or delete a line, creating content for a new file as well.
        if not matched and userline:
            new_lines.append(userline + eol)

        # Try to (re-)open file write-only now and save new content.
        try:
            _writelines(filename, new_lines)
        except EnvironmentError as e:
            if e.errno == errno.EACCES or e.errno == errno.EROFS:
                raise TracError(_(
                    "The password file could not be updated. Trac requires "
                    "read and write access to both the password file and its "
                    "parent directory."))
            else:
                raise
        return matched


class HtPasswdStore(AbstractPasswordFileStore):
    """Manages user accounts stored in Apache's htpasswd format.

    To use this implementation add the following configuration section to
    trac.ini:
    {{{
    [account-manager]
    password_store = HtPasswdStore
    htpasswd_file = /path/to/trac.htpasswd
    htpasswd_hash_type = crypt|md5|sha|sha256|sha512 <- None or one of these
    }}}

    Default behaviour is to detect presence of 'crypt' and use it or
    fallback to generation of passwords with md5 hash otherwise.
    """

    implements(IPasswordStore)

    filename = EnvRelativePathOption('account-manager', 'htpasswd_file', '',
        doc=N_("Path relative to Trac environment or full host machine path "
               "to password file"))
    hash_type = Option('account-manager', 'htpasswd_hash_type', 'crypt',
        doc=N_("Default hash type of new/updated passwords"))

    def config_key(self):
        return 'htpasswd'

    def prefix(self, user):
        return user + ':'

    def userline(self, user, password):
        return self.prefix(user) + generate_hash(password, self.hash_type)

    def _check_userline(self, user, password, suffix):
        return check_hash(password, suffix)

    def _get_users(self, filename):
        for line in _eachline(filename):
            user = line.split(':', 1)[0]
            if user:
                yield user


class HtDigestStore(AbstractPasswordFileStore):
    """Manages user accounts stored in Apache's htdigest format.

    To use this implementation add the following configuration section to
    trac.ini:
    {{{
    [account-manager]
    password_store = HtDigestStore
    htdigest_file = /path/to/trac.htdigest
    htdigest_realm = TracDigestRealm
    }}}
    """

    implements(IPasswordStore)

    filename = EnvRelativePathOption('account-manager', 'htdigest_file', '',
        doc=N_("Path relative to Trac environment or full host machine path "
               "to password file"))
    realm = Option('account-manager', 'htdigest_realm', '',
        doc=N_("Realm to select relevant htdigest file entries"))

    def config_key(self):
        return 'htdigest'

    def prefix(self, user):
        return '%s:%s:' % (user, self.realm)

    def userline(self, user, password):
        prefix = self.prefix(user)
        return prefix + htdigest(user, self.realm, password)

    def _check_userline(self, user, password, suffix):
        hash_ = htdigest(user, self.realm, password)
        return compare_digest(suffix, hash_)

    def _get_users(self, filename):
        for line in _eachline(filename):
            args = line.split(':')[:2]
            if len(args) == 2:
                user, realm = args
                if realm == self.realm and user:
                    yield user
