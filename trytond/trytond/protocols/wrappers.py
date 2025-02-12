# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
import base64
import gzip
import logging
import time
from functools import wraps
from io import BytesIO

try:
    from http import HTTPStatus
except ImportError:
    from http import client as HTTPStatus

from werkzeug import exceptions
from werkzeug.datastructures import Authorization
from werkzeug.exceptions import abort
from werkzeug.utils import redirect
from werkzeug.wrappers import Request as _Request
from werkzeug.wrappers import Response

from trytond import backend, security
from trytond.config import config
from trytond.exceptions import RateLimitException, UserError, UserWarning
from trytond.pool import Pool
from trytond.tools import cached_property
from trytond.transaction import Transaction, check_access

__all__ = [
    'HTTPStatus',
    'Request',
    'Response',
    'abort',
    'allow_null_origin',
    'exceptions',
    'redirect',
    'set_max_request_size',
    'user_application',
    'with_pool',
    'with_transaction',
    ]

logger = logging.getLogger(__name__)


class Request(_Request):

    view_args = None

    def __repr__(self):
        args = []
        try:
            if self.url is None or isinstance(self.url, str):
                url = self.url
            else:
                url = self.url.decode(self.url_charset)
            auth = self.authorization
            if auth:
                args.append("%s@%s" % (
                        auth.get('userid', auth.username), self.remote_addr))
            else:
                args.append(self.remote_addr)
            args.append("'%s'" % url)
            args.append("[%s]" % self.method)
            args.append("%s" % (self.rpc_method or ''))
        except Exception:
            args.append("(invalid WSGI environ)")
        return "<%s %s>" % (
            self.__class__.__name__, " ".join(filter(None, args)))

    @property
    def decoded_data(self):
        if self.content_encoding == 'gzip':
            zipfile = gzip.GzipFile(fileobj=BytesIO(self.data), mode='rb')
            return zipfile.read()
        else:
            return self.data

    @property
    def parsed_data(self):
        return self.data

    @property
    def rpc_method(self):
        return

    @property
    def rpc_params(self):
        return

    @cached_property
    def authorization(self):
        authorization = super(Request, self).authorization
        if authorization is None:
            header = self.environ.get('HTTP_AUTHORIZATION')
            return parse_authorization_header(header)
        return authorization

    @cached_property
    def user_id(self):
        if not self.view_args:
            return None
        database_name = self.view_args.get('database_name')
        if not database_name:
            return None
        auth = self.authorization
        if not auth:
            return None
        context = {'_request': self.context}
        if auth.type == 'session':
            user_id = security.check(
                database_name, auth.get('userid'), auth.get('session'),
                context=context)
        else:
            try:
                user_id = security.login(
                    database_name, auth.username, auth, cache=False,
                    context=context)
            except RateLimitException:
                abort(HTTPStatus.TOO_MANY_REQUESTS)
        return user_id

    @cached_property
    def context(self):
        return {
            'remote_addr': self.remote_addr,
            'http_host': self.environ.get('HTTP_HOST'),
            'scheme': self.scheme,
            'is_secure': self.is_secure,
            }


def parse_authorization_header(value):
    if not value:
        return
    if not isinstance(value, bytes):
        value = value.encode('latin1')
    try:
        auth_type, auth_info = value.split(None, 1)
        auth_type = auth_type.lower()
    except ValueError:
        return
    if auth_type == b'session':
        try:
            username, userid, session = base64.b64decode(auth_info).split(
                b':', 3)
            userid = int(userid)
        except Exception:
            return
        return Authorization('session', {
                'username': username.decode("latin1"),
                'userid': userid,
                'session': session.decode("latin1"),
                })


def set_max_request_size(size):
    def decorator(func):
        func.max_request_size = size
        return func
    return decorator


def allow_null_origin(func):
    func.allow_null_origin = True
    return func


def with_pool(func):
    @wraps(func)
    def wrapper(request, database_name, *args, **kwargs):
        database_list = Pool.database_list()
        pool = Pool(database_name)
        if database_name not in database_list:
            with Transaction().start(database_name, 0, readonly=True):
                pool.init()
        try:
            return func(request, pool, *args, **kwargs)
        except exceptions.HTTPException:
            logger.debug('%s', request, exc_info=True)
            raise
        except (UserError, UserWarning) as e:
            logger.debug('%s', request, exc_info=True)
            if request.rpc_method:
                raise
            else:
                abort(HTTPStatus.BAD_REQUEST, e)
        except Exception as e:
            logger.error('%s', request, exc_info=True)
            if request.rpc_method:
                raise
            else:
                abort(HTTPStatus.INTERNAL_SERVER_ERROR, e)
    return wrapper


def with_transaction(readonly=None, user=0, context=None):
    from trytond.worker import run_task

    def decorator(func):
        @wraps(func)
        def wrapper(request, pool, *args, **kwargs):
            readonly_ = readonly  # can not modify non local
            if readonly_ is None:
                if request.method in {'POST', 'PUT', 'DELETE', 'PATCH'}:
                    readonly_ = False
                else:
                    readonly_ = True
            if context is None:
                context_ = {}
            else:
                context_ = context.copy()
            context_['_request'] = request.context
            if user == 'request':
                user_ = request.user_id
            else:
                user_ = user
            retry = config.getint('database', 'retry')
            for count in range(retry, -1, -1):
                if count != retry:
                    time.sleep(0.02 * (retry - count))
                with Transaction().start(
                        pool.database_name, user_, readonly=readonly_,
                        context=context_) as transaction:
                    try:
                        result = func(request, pool, *args, **kwargs)
                    except backend.DatabaseOperationalError:
                        if count and not readonly_:
                            transaction.rollback()
                            continue
                        raise
                    # Need to commit to unlock SQLite database
                    transaction.commit()
                while transaction.tasks:
                    task_id = transaction.tasks.pop()
                    run_task(pool, task_id)
                return result
        return wrapper
    return decorator


def user_application(name, json=True):
    from .jsonrpc import JSONEncoder
    from .jsonrpc import json as json_

    def decorator(func):
        @wraps(func)
        def wrapper(request, *args, **kwargs):
            pool = Pool()
            UserApplication = pool.get('res.user.application')

            authorization = request.headers['Authorization']
            try:
                auth_type, auth_info = authorization.split(None, 1)
                auth_type = auth_type.lower()
            except ValueError:
                abort(HTTPStatus.UNAUTHORIZED)
            if auth_type != 'bearer':
                abort(HTTPStatus.FORBIDDEN)

            application = UserApplication.check(auth_info, name)
            if not application:
                abort(HTTPStatus.FORBIDDEN)
            transaction = Transaction()
            # TODO language
            with transaction.set_user(application.user.id), \
                    check_access():
                response = func(request, *args, **kwargs)
            if not isinstance(response, Response) and json:
                response = Response(json_.dumps(response, cls=JSONEncoder),
                    content_type='application/json')
            return response
        return wrapper
    return decorator
