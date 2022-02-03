# pylint: disable=redefined-outer-name

import logging
from aiohttp import web
import pytest

from flaat.aio import Flaat
from flaat.test_env import User, STATUS_KWARGS_LIST, FLAAT_TRUSTED_OPS_LIST

flaat = Flaat()
flaat.set_trusted_OP_list(FLAAT_TRUSTED_OPS_LIST)

DECORATORS = User(flaat).get_named_decorators()

logger = logging.getLogger(__name__)


async def view_func(request, test_inject=None):
    _ = request
    _ = test_inject
    return web.Response(text="Success")


@pytest.fixture
def app():
    """aio web Application for testing"""
    app = web.Application()
    for decorator in DECORATORS:
        decorated = decorator.decorator(view_func)
        app.router.add_get(f"/{decorator.name}", decorated)
    return app


# from: https://docs.aiohttp.org/en/stable/testing.html#pytest-example
@pytest.fixture
def cli(event_loop, aiohttp_client, app):
    return event_loop.run_until_complete(aiohttp_client(app))


@pytest.mark.parametrize("status,kwargs", STATUS_KWARGS_LIST)
@pytest.mark.parametrize("decorator", DECORATORS)
async def test_decorator(cli, decorator, status, kwargs):
    logger.debug("Decorator: %s", decorator.name)
    resp = await cli.get(f"/{decorator.name}", **kwargs)
    expected = decorator.get_expected_status_code(status)
    assert resp.status == expected
