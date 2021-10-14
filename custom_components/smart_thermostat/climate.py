"""
Adds support for smart thermostat units.
forked from HA-core `generic_thermostat` 827501659c926ace3741425760b1294d2e93b48e
"""
import asyncio
import logging
import math
from typing import Optional

import voluptuous as vol

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    ATTR_PRESET_MODE,
    CURRENT_HVAC_COOL,
    CURRENT_HVAC_HEAT,
    CURRENT_HVAC_IDLE,
    CURRENT_HVAC_OFF,
    HVAC_MODE_COOL,
    HVAC_MODE_HEAT,
    HVAC_MODE_HEAT_COOL,
    HVAC_MODE_OFF,
    PRESET_AWAY,
    PRESET_NONE,
    SUPPORT_PRESET_MODE,
)
from homeassistant.const import (
    ATTR_TEMPERATURE,
    CONF_ENTITY_ID,
    CONF_NAME,
    CONF_UNIQUE_ID,
    EVENT_HOMEASSISTANT_START,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import CoreState, callback, Context, HomeAssistant
from homeassistant.helpers.entity_platform import EntityPlatform
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.reload import async_setup_reload_service
from homeassistant.helpers.restore_state import RestoreEntity
from . import DOMAIN, PLATFORMS
from .config import (
    CONF_HEATER,
    CONF_COOLER,
    CONF_SENSOR,
    CONF_MIN_TEMP,
    CONF_MAX_TEMP,
    CONF_TARGET_TEMP,
    CONF_COLD_TOLERANCE,
    CONF_HOT_TOLERANCE,
    CONF_KEEP_ALIVE,
    CONF_INITIAL_HVAC_MODE,
    CONF_AWAY_TEMP,
    CONF_PRECISION,
    SUPPORT_FLAGS,
    TARGET_SCHEMA,
    KEY_SCHEMA,
    DATA_SCHEMA
)
from .controllers import SwitchController

_LOGGER = logging.getLogger(__name__)

PLATFORM_SCHEMA = vol.All(KEY_SCHEMA, DATA_SCHEMA)


def _extract_target(target):
    if isinstance(target, str):
        return TARGET_SCHEMA({
            CONF_ENTITY_ID: target
        })
    else:
        return target


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up the smart thermostat platform."""

    # prevent unused variable warn
    _ = discovery_info

    await async_setup_reload_service(hass, DOMAIN, PLATFORMS)

    name = config.get(CONF_NAME)
    heater = _extract_target(config.get(CONF_HEATER))
    cooler = _extract_target(config.get(CONF_COOLER))
    sensor_entity_id = config.get(CONF_SENSOR)
    min_temp = config.get(CONF_MIN_TEMP)
    max_temp = config.get(CONF_MAX_TEMP)
    target_temp = config.get(CONF_TARGET_TEMP)
    cold_tolerance = config.get(CONF_COLD_TOLERANCE)
    hot_tolerance = config.get(CONF_HOT_TOLERANCE)
    keep_alive = config.get(CONF_KEEP_ALIVE)
    initial_hvac_mode = config.get(CONF_INITIAL_HVAC_MODE)
    away_temp = config.get(CONF_AWAY_TEMP)
    precision = config.get(CONF_PRECISION)
    unit = hass.config.units.temperature_unit
    unique_id = config.get(CONF_UNIQUE_ID)

    async_add_entities(
        [
            SmartThermostat(
                name,
                heater,
                cooler,
                sensor_entity_id,
                min_temp,
                max_temp,
                target_temp,
                cold_tolerance,
                hot_tolerance,
                keep_alive,
                initial_hvac_mode,
                away_temp,
                precision,
                unit,
                unique_id,
            )
        ]
    )


# noinspection PyAbstractClass
class SmartThermostat(ClimateEntity, RestoreEntity):
    """Representation of a Smart Thermostat device."""

    def __init__(
            self,
            name,
            heater,
            cooler,
            sensor_entity_id,
            min_temp,
            max_temp,
            target_temp,
            cold_tolerance,
            hot_tolerance,
            keep_alive,
            initial_hvac_mode,
            away_temp,
            precision,
            unit,
            unique_id,
    ):
        """Initialize the thermostat."""
        self._name = name
        self.sensor_entity_id = sensor_entity_id
        self._keep_alive = keep_alive
        self._hvac_mode = initial_hvac_mode
        self._saved_target_temp = target_temp or away_temp
        self._temp_precision = precision
        self._hvac_list = [HVAC_MODE_OFF]
        self._cur_temp = None
        self._temp_lock = asyncio.Lock()
        self._min_temp = min_temp
        self._max_temp = max_temp
        self._attr_preset_mode = PRESET_NONE
        self._target_temp = target_temp
        self._unit = unit
        self._unique_id = unique_id
        self._support_flags = SUPPORT_FLAGS
        if away_temp:
            self._support_flags = SUPPORT_FLAGS | SUPPORT_PRESET_MODE
            self._attr_preset_modes = [PRESET_NONE, PRESET_AWAY]
        else:
            self._attr_preset_modes = [PRESET_NONE]
        self._away_temp = away_temp

        self._controllers = []

        # Create cooler
        if cooler is not None:
            self._cooler = SwitchController(
                'cooler',
                [HVAC_MODE_COOL, HVAC_MODE_HEAT_COOL],
                HVAC_MODE_COOL,
                cooler,
                cold_tolerance,
                hot_tolerance
            )
            self._controllers.append(self._cooler)
            self._hvac_list.append(HVAC_MODE_COOL)
            _LOGGER.info(f"Added cooler: {cooler}")
        else:
            self._cooler = None

        # Create heater
        if heater is not None:
            self._heater = SwitchController(
                'heater',
                [HVAC_MODE_HEAT, HVAC_MODE_HEAT_COOL],
                HVAC_MODE_HEAT,
                heater,
                cold_tolerance,
                hot_tolerance
            )
            self._controllers.append(self._heater)
            self._hvac_list.append(HVAC_MODE_HEAT)
            _LOGGER.info(f"Added heater: {heater}")
        else:
            self._heater = None

        if self._cooler and self._cooler:
            self._hvac_list.append(HVAC_MODE_HEAT_COOL)
            _LOGGER.info(f"Allowed Heat/Cool mode")

    async def async_added_to_hass(self):
        """Run when entity about to be added."""
        await super().async_added_to_hass()

        # Add listener
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self.sensor_entity_id], self._async_sensor_changed
            )
        )

        for controller in self._controllers:
            def state_changed_callback(event):
                controller.on_state_changed(event)
                self.async_write_ha_state()

            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, controller.get_entities_to_subscribe_state_changes(), state_changed_callback
                )
            )

        if self._keep_alive:
            self.async_on_remove(
                async_track_time_interval(
                    self.hass, self._async_control, self._keep_alive
                )
            )

        @callback
        def _async_startup(*_):
            """Init on startup."""
            sensor_state = self.hass.states.get(self.sensor_entity_id)
            if sensor_state and sensor_state.state not in (
                    STATE_UNAVAILABLE,
                    STATE_UNKNOWN,
            ):
                self._async_update_temp(sensor_state)
                self.async_write_ha_state()

                for contr in self._controllers:
                    contr.startup()

        if self.hass.state == CoreState.running:
            _async_startup()
        else:
            self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START, _async_startup)

        # Check If we have an old state
        old_state = await self.async_get_last_state()
        if old_state is not None:
            # If we have no initial temperature, restore
            if self._target_temp is None:
                # If we have a previously saved temperature
                if old_state.attributes.get(ATTR_TEMPERATURE) is None:
                    self._target_temp = self._get_default_target_temp()
                    _LOGGER.warning(
                        "Undefined target temperature, falling back to %s",
                        self._target_temp,
                    )
                else:
                    self._target_temp = float(old_state.attributes[ATTR_TEMPERATURE])
            if old_state.attributes.get(ATTR_PRESET_MODE) in self._attr_preset_modes:
                self._attr_preset_mode = old_state.attributes.get(ATTR_PRESET_MODE)
            if not self._hvac_mode and old_state.state:
                self._set_hvac_mode(old_state.state)

        else:
            # No previous state, try and restore defaults
            self._target_temp = self._get_default_target_temp()
            _LOGGER.warning(
                "No previously saved temperature, setting to %s", self._target_temp
            )

        # Set default state to off
        if not self._hvac_mode:
            self._set_hvac_mode(HVAC_MODE_OFF)

    def add_to_platform_start(self, hass: HomeAssistant, platform: EntityPlatform, parallel_updates: Optional[asyncio.Semaphore]) -> None:
        # FIXME: Use more ellegant way to receive hass/context
        super().add_to_platform_start(hass, platform, parallel_updates)
        for controller in self._controllers:
            controller.set_hass(hass)

    def async_set_context(self, context: Context) -> None:
        # FIXME: Use more ellegant way to receive hass/context
        super().async_set_context(context)
        for controller in self._controllers:
            controller.set_context(context)

    def _get_default_target_temp(self):
        return (self.max_temp + self.min_temp) / 2

    @property
    def should_poll(self):
        """Return the polling state."""
        return False

    @property
    def name(self):
        """Return the name of the thermostat."""
        return self._name

    @property
    def unique_id(self):
        """Return the unique id of this thermostat."""
        return self._unique_id

    @property
    def precision(self):
        """Return the precision of the system."""
        if self._temp_precision is not None:
            return self._temp_precision
        return super().precision

    @property
    def target_temperature_step(self):
        """Return the supported step of target temperature."""
        # Since this integration does not yet have a step size parameter
        # we have to re-use the precision as the step size for now.
        return self.precision

    @property
    def temperature_unit(self):
        """Return the unit of measurement."""
        return self._unit

    @property
    def current_temperature(self):
        """Return the sensor temperature."""
        return self._cur_temp

    @property
    def hvac_mode(self):
        """Return current operation."""
        return self._hvac_mode

    @property
    def hvac_action(self):
        """Return the current running hvac operation if supported.

        Need to be one of CURRENT_HVAC_*.
        """
        if self._hvac_mode == HVAC_MODE_OFF:
            return CURRENT_HVAC_OFF
        if self._cooler and self._cooler.running:
            return CURRENT_HVAC_COOL
        if self._heater and self._heater.running:
            return CURRENT_HVAC_HEAT
        return CURRENT_HVAC_IDLE

    @property
    def target_temperature(self):
        """Return the temperature we try to reach."""
        return self._target_temp

    @property
    def hvac_modes(self):
        """List of available operation modes."""
        return self._hvac_list

    def _set_hvac_mode(self, hvac_mode: str):
        _LOGGER.info(f"Setting HVAC mode from {self._hvac_mode} to {hvac_mode}")
        self._hvac_mode = hvac_mode
        for controller in self._controllers:
            controller.set_hvac_mode(hvac_mode)

    async def async_set_hvac_mode(self, hvac_mode):
        """Set hvac mode."""
        self._set_hvac_mode(hvac_mode)

        if hvac_mode not in self._hvac_list:
            _LOGGER.error("Unrecognized hvac mode: %s", hvac_mode)
            return

        await self._async_control(force=True)

        # Ensure we update the current operation after changing the mode
        self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs):
        """Set new target temperature."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        self._target_temp = temperature
        await self._async_control(force=True)
        self.async_write_ha_state()

    @property
    def min_temp(self):
        """Return the minimum temperature."""
        if self._min_temp is not None:
            return self._min_temp

        # get default temp from super class
        return super().min_temp

    @property
    def max_temp(self):
        """Return the maximum temperature."""
        if self._max_temp is not None:
            return self._max_temp

        # Get default temp from super class
        return super().max_temp

    async def _async_sensor_changed(self, event):
        """Handle temperature changes."""
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return

        self._async_update_temp(new_state)
        await self._async_control()
        self.async_write_ha_state()

    @callback
    def _async_update_temp(self, state):
        """Update thermostat with latest state from sensor."""
        try:
            cur_temp = float(state.state)
            if math.isnan(cur_temp) or math.isinf(cur_temp):
                raise ValueError(f"Sensor has illegal state {state.state}")
            self._cur_temp = cur_temp
        except ValueError as ex:
            _LOGGER.error("Unable to update from sensor: %s", ex)

    async def _async_control(self, time=None, force=False):
        """Call controllers"""
        async with self._temp_lock:
            for controller in self._controllers:
                await controller.async_control(self._cur_temp, self._target_temp, time=time, force=force)

    @property
    def supported_features(self):
        """Return the list of supported features."""
        return self._support_flags

    async def async_set_preset_mode(self, preset_mode: str):
        """Set new preset mode."""
        if preset_mode not in (self._attr_preset_modes or []):
            raise ValueError(
                f"Got unsupported preset_mode {preset_mode}. Must be one of {self._attr_preset_modes}"
            )
        if preset_mode == self._attr_preset_mode:
            # I don't think we need to call async_write_ha_state if we didn't change the state
            return
        if preset_mode == PRESET_AWAY:
            self._attr_preset_mode = PRESET_AWAY
            self._saved_target_temp = self._target_temp
            self._target_temp = self._away_temp
            await self._async_control(force=True)
        elif preset_mode == PRESET_NONE:
            self._attr_preset_mode = PRESET_NONE
            self._target_temp = self._saved_target_temp
            await self._async_control(force=True)

        self.async_write_ha_state()
