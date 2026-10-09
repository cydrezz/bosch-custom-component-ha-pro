"""Test Bosch sensors."""
from datetime import timedelta
from unittest.mock import MagicMock

from homeassistant.const import CONF_ADDRESS, CONF_PASSWORD, UnitOfTemperature
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.bosch.const import (
    ACCESS_KEY,
    ACCESS_TOKEN,
    CONF_DEVICE_TYPE,
    CONF_PROTOCOL,
    DOMAIN,
    ENERGY_COUNTER_UNITS,
    SENSORS,
    UUID,
)
from custom_components.bosch.sensor.base import convert_energy_counter
from custom_components.bosch.sensor.bosch import BoschSensor


def test_energy_counter_unit_divisors():
    """Raw counter is scaled to kWh by the per-gateway option; kWh = unchanged."""
    assert convert_energy_counter(42483056, ENERGY_COUNTER_UNITS["J"]) == 11.801
    assert convert_energy_counter(431.4, ENERGY_COUNTER_UNITS["kWh"]) == 431.4
    assert convert_energy_counter(28128234, ENERGY_COUNTER_UNITS["Wh"]) == 28128.234
    assert convert_energy_counter(3600, ENERGY_COUNTER_UNITS["kJ"]) == 1.0
    assert convert_energy_counter("n/a", 1) == "n/a"


def _energy_sensor(**kwargs):
    obj = MagicMock()
    obj.parent_id = None
    obj.id = "energyConsumption"
    obj.name = "Energy consumption"
    obj.device_class = "energy"
    obj.state_class = "total"
    obj.entity_category = None
    return BoschSensor(
        hass=MagicMock(),
        uuid="123456789-demo",
        bosch_object=obj,
        gateway=MagicMock(),
        name=obj.name,
        attr_uri="energyConsumption",
        **kwargs,
    )


def test_energy_counter_unit_reaches_sensor():
    """The per-entry option sets the divisor; without it the raw value stays."""
    assert _energy_sensor(energy_counter_unit="J")._energy_divisor == 3_600_000
    assert _energy_sensor(energy_counter_unit="Wh")._energy_divisor == 1_000
    assert _energy_sensor()._energy_divisor == 1

async def test_sensor_mock_values(hass):
    """Test that sensors are created with mock values."""
    # Setup the config entry with demo credentials
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_ADDRESS: "demo",
            ACCESS_TOKEN: "demo",
            ACCESS_KEY: "demo_key",
            CONF_PASSWORD: "demo",
            CONF_DEVICE_TYPE: "IVT",
            CONF_PROTOCOL: "HTTP",
            UUID: "123456789-demo",
            SENSORS: ["actualSupplyTemperature", "outdoorTemperature"]
        }
    )
    entry.add_to_hass(hass)

    # Load the integration
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    
    # Advance time to trigger the initial update (clears lingering timer and updates entities)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=10))
    await hass.async_block_till_done()

    print(f"All states: {hass.states.async_all()}")
    # Check Actual Supply Temperature (Mock value: 27.8)
    state = hass.states.get("sensor.actual_supply_temp")
    assert state is not None
    assert state.state == "27.8"
    assert state.attributes["unit_of_measurement"] == UnitOfTemperature.CELSIUS

    # Check Outdoor Temperature (Mock value: 3.5)
    state = hass.states.get("sensor.outdoor_temperature")
    assert state is not None
    assert state.state == "3.5"
    assert state.attributes["unit_of_measurement"] == UnitOfTemperature.CELSIUS