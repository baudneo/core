"""Support for VeSync numeric entities."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging
import time

from pyvesync.base_devices import VeSyncBaseDevice
from pyvesync.device_container import DeviceContainer

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .common import is_humidifier, is_outlet, is_purifier, is_evaporative_humidifier
from .const import (
    HUMIDIFIER_NIGHT_LIGHT_LEVEL_BRIGHT,
    HUMIDIFIER_NIGHT_LIGHT_LEVEL_DIM,
    HUMIDIFIER_NIGHT_LIGHT_LEVEL_OFF,
    OUTLET_NIGHT_LIGHT_LEVEL_AUTO,
    OUTLET_NIGHT_LIGHT_LEVEL_OFF,
    OUTLET_NIGHT_LIGHT_LEVEL_ON,
    PURIFIER_NIGHT_LIGHT_LEVEL_DIM,
    PURIFIER_NIGHT_LIGHT_LEVEL_OFF,
    PURIFIER_NIGHT_LIGHT_LEVEL_ON,
    VS_DEVICES,
    VS_DISCOVERY, HUMIDIFIER_DRYING_LEVEL_OFF, HUMIDIFIER_DRYING_LEVEL_LOW, HUMIDIFIER_DRYING_LEVEL_HIGH,
)
from .coordinator import VesyncConfigEntry, VeSyncDataCoordinator
from .entity import VeSyncBaseEntity

_LOGGER = logging.getLogger(__name__)

VS_TO_HA_HUMIDIFIER_NIGHT_LIGHT_LEVEL_MAP = {
    100: HUMIDIFIER_NIGHT_LIGHT_LEVEL_BRIGHT,
    50: HUMIDIFIER_NIGHT_LIGHT_LEVEL_DIM,
    0: HUMIDIFIER_NIGHT_LIGHT_LEVEL_OFF,
}

HA_TO_VS_HUMIDIFIER_NIGHT_LIGHT_LEVEL_MAP = {
    v: k for k, v in VS_TO_HA_HUMIDIFIER_NIGHT_LIGHT_LEVEL_MAP.items()
}

# Fan Level Mapping for Evaporative Humidifiers (Levoit 6000S)
# Backend expects 1-9. We map ranges to Low/Med/High.
FAN_LEVEL_LOW = "low"
FAN_LEVEL_MEDIUM = "medium"
FAN_LEVEL_HIGH = "high"

PARALLEL_UPDATES = 1


def _set_humidifier_nightlight(device: VeSyncBaseDevice, *args) -> Awaitable[bool]:
    """Toggle humidifier nightlight on."""
    if is_humidifier(device):
        return device.set_nightlight_brightness(*args)
    raise HomeAssistantError("Device does not support toggling nightlight.")


def _toggle_purifier_nightlight(device: VeSyncBaseDevice, *args) -> Awaitable[bool]:
    """Toggle air purifier nightlight on."""
    if is_purifier(device):
        return device.set_nightlight_mode(*args)
    raise HomeAssistantError("Device does not support toggling nightlight.")


def _toggle_outlet_nightlight(device: VeSyncBaseDevice, *args) -> Awaitable[bool]:
    """Toggle outlet nightlight on."""
    if is_outlet(device) and device.supports_nightlight:
        return device.set_nightlight_state(*args)
    raise HomeAssistantError("Device does not support toggling nightlight.")


async def _set_drying_mode(device: VeSyncBaseDevice, value: str) -> bool:
    """Set drying mode with specific level using raw API call."""
    if value == "off":
        _LOGGER.debug("Drying Mode 'Off' selected. Stopping drying.")
        # All 3 of these are required, another option is to just turn_off()
        json_body = {
            "enabled": False,
            "dryingState": 0,
            "dryingLevel": 0
        }
    else:
        # Map selection to integer level (Low=1, High=2)
        level_int = 2 if value == "high" else 1
        json_body = {
            "enabled": True,
            "dryingLevel": level_int
        }
    _LOGGER.debug("Sending raw setDryingMode: %s", json_body)

    try:
        response = await device.call_bypassv2_api("setDryingMode", json_body)
        return response.get("code", -1) == 0
    except Exception as e:
        _LOGGER.error("Failed to set drying mode raw: %s", e)
        return False


def _get_drying_current_option(entity: VeSyncBaseEntity) -> str:
    """Calculate current option with optimistic override."""
    # Check optimistic "forced off" timestamp from coordinator (~3 mins)
    last_action = entity.coordinator.device_last_action.get(entity.device.cid, 0)
    if (time.time() - last_action) < 190:
        return "off"

    # Fallback to cloud state
    if not getattr(entity.device.state, "drying_mode_running", False):
        return "off"

    # If running, return level
    level = getattr(entity.device.state, "drying_mode_level", 1)
    return "high" if level == 2 else "low"


async def _set_fan_level(device: VeSyncBaseDevice, value: str) -> bool:
    """Set fan level for evaporative humidifiers."""
    if value == FAN_LEVEL_LOW:
        level = 3
    elif value == FAN_LEVEL_MEDIUM:
        level = 6
    else:
        level = 9

    return await device.set_mist_level(level)


def _get_fan_level_option(entity: VeSyncBaseEntity) -> str:
    """Get current fan level option."""
    level = entity.device.state.mist_virtual_level
    if level <= 3:
        return FAN_LEVEL_LOW
    if level <= 6:
        return FAN_LEVEL_MEDIUM
    return FAN_LEVEL_HIGH


@dataclass(frozen=True, kw_only=True)
class VeSyncSelectEntityDescription(SelectEntityDescription):
    """Class to describe a Vesync select entity."""

    exists_fn: Callable[[VeSyncBaseDevice], bool]
    current_option_fn: Callable[[VeSyncBaseEntity], str]
    select_option_fn: Callable[[VeSyncBaseDevice, str], Awaitable[bool]]


SELECT_DESCRIPTIONS: list[VeSyncSelectEntityDescription] = [
    # night_light for humidifier
    VeSyncSelectEntityDescription(
        key="night_light_level",
        translation_key="night_light_level",
        options=list(VS_TO_HA_HUMIDIFIER_NIGHT_LIGHT_LEVEL_MAP.values()),
        icon="mdi:brightness-6",
        exists_fn=lambda device: is_humidifier(device) and device.supports_nightlight,
        select_option_fn=lambda device, value: _set_humidifier_nightlight(
            device, HA_TO_VS_HUMIDIFIER_NIGHT_LIGHT_LEVEL_MAP.get(value, 0)
        ),
        current_option_fn=lambda entity: VS_TO_HA_HUMIDIFIER_NIGHT_LIGHT_LEVEL_MAP.get(
            entity.device.state.nightlight_brightness,
            HUMIDIFIER_NIGHT_LIGHT_LEVEL_OFF,
        ),
    ),
    # night_light for air purifiers
    VeSyncSelectEntityDescription(
        key="night_light_level",
        translation_key="night_light_level",
        options=[
            PURIFIER_NIGHT_LIGHT_LEVEL_OFF,
            PURIFIER_NIGHT_LIGHT_LEVEL_DIM,
            PURIFIER_NIGHT_LIGHT_LEVEL_ON,
        ],
        icon="mdi:brightness-6",
        exists_fn=lambda device: is_purifier(device) and device.supports_nightlight,
        select_option_fn=_toggle_purifier_nightlight,
        current_option_fn=lambda entity: entity.device.state.nightlight_status,
    ),
    # night_light for outlets
    VeSyncSelectEntityDescription(
        key="night_light_level",
        translation_key="night_light_level",
        options=[
            OUTLET_NIGHT_LIGHT_LEVEL_OFF,
            OUTLET_NIGHT_LIGHT_LEVEL_ON,
            OUTLET_NIGHT_LIGHT_LEVEL_AUTO,
        ],
        icon="mdi:brightness-6",
        exists_fn=lambda device: is_outlet(device) and device.supports_nightlight,
        select_option_fn=_toggle_outlet_nightlight,
        current_option_fn=lambda entity: entity.device.state.nightlight_status,
    ),
    # drying_mode for humidifiers (confirmed Levoit 6000S)
    VeSyncSelectEntityDescription(
        key="drying_mode",
        translation_key="drying_mode",
        options=[
            HUMIDIFIER_DRYING_LEVEL_OFF,
            HUMIDIFIER_DRYING_LEVEL_LOW,
            HUMIDIFIER_DRYING_LEVEL_HIGH
        ],
        icon="mdi:weather-sunny",
        exists_fn=lambda device: is_humidifier(device) and getattr(device, "supports_drying_mode", False),
        select_option_fn=_set_drying_mode,
        current_option_fn=_get_drying_current_option,
    ),
    # Fan Level for Evaporative Humidifiers (confirmed Levoit 6000S)
    VeSyncSelectEntityDescription(
        key="fan_level",
        translation_key="fan_level",
        options=[FAN_LEVEL_LOW, FAN_LEVEL_MEDIUM, FAN_LEVEL_HIGH],
        icon="mdi:fan",
        exists_fn=lambda device: is_evaporative_humidifier(device),
        select_option_fn=_set_fan_level,
        current_option_fn=_get_fan_level_option,
    ),
]


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: VesyncConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up select entities."""

    coordinator = config_entry.runtime_data

    @callback
    def discover(devices: list[VeSyncBaseDevice]) -> None:
        """Add new devices to platform."""
        _setup_entities(devices, async_add_entities, coordinator)

    config_entry.async_on_unload(
        async_dispatcher_connect(hass, VS_DISCOVERY.format(VS_DEVICES), discover)
    )

    _setup_entities(
        config_entry.runtime_data.manager.devices, async_add_entities, coordinator
    )


@callback
def _setup_entities(
    devices: DeviceContainer | list[VeSyncBaseDevice],
    async_add_entities: AddConfigEntryEntitiesCallback,
    coordinator: VeSyncDataCoordinator,
) -> None:
    """Add select entities."""

    async_add_entities(
        VeSyncSelectEntity(dev, description, coordinator)
        for dev in devices
        for description in SELECT_DESCRIPTIONS
        if description.exists_fn(dev)
    )


class VeSyncSelectEntity(VeSyncBaseEntity, SelectEntity):
    """A class to set numeric options on Vesync device."""

    entity_description: VeSyncSelectEntityDescription

    def __init__(
        self,
        device: VeSyncBaseDevice,
        description: VeSyncSelectEntityDescription,
        coordinator: VeSyncDataCoordinator,
    ) -> None:
        """Initialize the VeSync select device."""
        super().__init__(device, coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{super().unique_id}-{description.key}"

    @property
    def current_option(self) -> str | None:
        """Return an option."""
        return self.entity_description.current_option_fn(self)

    async def async_select_option(self, option: str) -> None:
        """Set an option."""
        if not await self.entity_description.select_option_fn(self.device, option):
            raise HomeAssistantError(self.device.last_response.message)
        # Update timestamp in coordinator if this is drying mode or fan level
        # If we change fan level, we assume drying is interrupted/off.
        if self.entity_description.key == "drying_mode":
            if option == "off":
                self.coordinator.device_last_action[self.device.cid] = time.time()
            else:
                if self.device.cid in self.coordinator.device_last_action:
                    del self.coordinator.device_last_action[self.device.cid]
        elif self.entity_description.key == "fan_level":
            # Changing fan level implies operation -> clear drying mode
            if hasattr(self.device.state, "drying_mode_running"):
                self.coordinator.device_last_action[self.device.cid] = time.time()

        self.async_write_ha_state()
