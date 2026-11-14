"""Tests for the custom PUT services: a failed PUT must be visible."""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import ATTR_DEVICE_ID
from homeassistant.exceptions import HomeAssistantError

from custom_components.bosch import BoschGatewayEntry
from custom_components.bosch.const import SERVICE_PUT_FLOAT
from custom_components.bosch.services import async_register_services

PATH = "/heatingCircuits/hc1/temperatureLevels/comfort2"


def _put_handler():
    """Return the service handler registered for the float PUT service."""
    hass = MagicMock()
    async_register_services(hass, MagicMock())
    for call in hass.services.async_register.call_args_list:
        if call.args[1] == SERVICE_PUT_FLOAT:
            return call.args[2]
    raise AssertionError("PUT service not registered")


def _gateway_entry(uuid, raw_put_result):
    """Gateway entry whose client library answers raw_put with the result."""
    entry = object.__new__(BoschGatewayEntry)
    entry.uuid = uuid
    entry._update_lock = asyncio.Lock()
    entry.gateway = MagicMock()
    entry.gateway.raw_put = AsyncMock(return_value=raw_put_result)
    return entry


async def _call_put(*entries):
    service_call = MagicMock()
    service_call.data = {ATTR_DEVICE_ID: ["device"], "path": PATH, "value": 21.5}
    with patch(
        "custom_components.bosch.services.find_gateway_entry",
        return_value=list(entries),
    ):
        return await _put_handler()(service_call)


async def test_confirmed_put_returns_library_result():
    """True from the library = gateway confirmed the write."""
    entry = _gateway_entry("gw1", True)

    result = await _call_put(entry)

    entry.gateway.raw_put.assert_awaited_once_with(path=PATH, value=21.5)
    assert result == {"data": [True]}


async def test_failed_put_raises_and_logs_error(caplog):
    """The library swallows DeviceException and returns None."""
    entry = _gateway_entry("gw1", None)

    with caplog.at_level(logging.ERROR), pytest.raises(HomeAssistantError):
        await _call_put(entry)

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any(PATH in r.getMessage() and "gw1" in r.getMessage() for r in errors)


async def test_all_gateways_are_tried_before_raising():
    failing = _gateway_entry("gw1", None)
    working = _gateway_entry("gw2", True)

    with pytest.raises(HomeAssistantError, match="gw1"):
        await _call_put(failing, working)

    working.gateway.raw_put.assert_awaited_once()


async def test_unclassifiable_http_answer_only_warns(caplog):
    """HTTP gateways: body without status code is neither success nor error."""
    entry = _gateway_entry("gw1", "")

    with caplog.at_level(logging.WARNING):
        result = await _call_put(entry)

    assert result == {"data": [""]}
    assert "not confirmed" in caplog.text
