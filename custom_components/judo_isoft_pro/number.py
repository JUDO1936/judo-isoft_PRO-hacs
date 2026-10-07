"""Writable JUDO settings exposed as Home Assistant slider numbers."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CMD_HARDNESS,
    CMD_HARDNESS_WRITE,
    CMD_MAX_DRAW_AMOUNT,
    CMD_MAX_FLOW,
    CMD_SALT_WARNING,
    DOMAIN,
    entity_prefix,
)
from .coordinator import JudoCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: JudoCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            JudoWritableNumber(
                coordinator, entry, "Salzmangel-Warnschwelle", CMD_SALT_WARNING, 0, 30, 1,
                "d", "mdi:alert-circle-outline", data_bytes=1,
                entity_key="salzmangel_warnschwelle", unique_suffix="57_number",
            ),
            JudoWritableNumber(
                coordinator, entry, "Wunschwasserhärte", CMD_HARDNESS, 0, 30, 1,
                None, "mdi:water-opacity", data_bytes=2,
                entity_key="wunschwasserharte", unique_suffix="51_number",
                write_command=CMD_HARDNESS_WRITE,
            ),
            JudoWritableNumber(
                coordinator, entry, "Max. Entnahmemenge", CMD_MAX_DRAW_AMOUNT, 0, 3000, 100,
                "L", "mdi:cup-water", data_bytes=2,
                entity_key="max_entnahmemenge", unique_suffix="3f_number",
            ),
            JudoWritableNumber(
                coordinator, entry, "Max. Volumenstrom", CMD_MAX_FLOW, 0, 5000, 250,
                "L/h", "mdi:water-flow", data_bytes=2,
                entity_key="max_volumenstrom", unique_suffix="40_number",
            ),
            JudoHolidayDaysNumber(coordinator, entry),
        ]
    )


class JudoWritableNumber(CoordinatorEntity[JudoCoordinator], NumberEntity):
    """A JUDO writable value rendered as a slider."""

    _attr_has_entity_name = True
    _attr_mode = NumberMode.SLIDER
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: JudoCoordinator,
        entry: ConfigEntry,
        name: str,
        read_command: str,
        minimum: float,
        maximum: float,
        step: float,
        unit: str | None,
        icon: str,
        *,
        data_bytes: int,
        entity_key: str,
        unique_suffix: str,
        write_command: str | None = None,
        read_bytes: int | None = None,
        read_scale: float = 1.0,
        write_scale: float = 1.0,
    ) -> None:
        super().__init__(coordinator)
        self._read_command = read_command
        self._write_command = write_command or read_command
        self._data_bytes = data_bytes
        self._read_bytes = read_bytes
        self._read_scale = read_scale
        self._write_scale = write_scale
        self._attr_name = name
        self._attr_unique_id = f"{DOMAIN}_{unique_suffix}_{entry.entry_id}"
        self._attr_default_entity_id = (
            f"number.{entity_prefix(coordinator.api.host, coordinator.api.port)}_{entity_key}"
        )
        self._attr_native_min_value = minimum
        self._attr_native_max_value = maximum
        self._attr_native_step = step
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": coordinator.device_name,
            "manufacturer": "JUDO Wasseraufbereitung",
        }

    @property
    def available(self) -> bool:
        return self.coordinator.available

    @property
    def native_value(self) -> float | None:
        raw = self.coordinator.value(self._read_command)
        if not raw:
            return None
        try:
            data = bytes.fromhex(raw)
            if self._read_bytes is not None:
                data = data[: self._read_bytes]
            value = int.from_bytes(data, "little") * self._read_scale
        except ValueError:
            return None
        return round(value, 3)

    async def async_set_native_value(self, value: float) -> None:
        device_value = int(round(value * self._write_scale))
        maximum = (1 << (self._data_bytes * 8)) - 1
        if not 0 <= device_value <= maximum:
            raise ValueError(
                f"Wert {value} ist für {self._data_bytes} Byte außerhalb des Bereichs."
            )
        data = device_value.to_bytes(self._data_bytes, "little", signed=False).hex().upper()
        await self.coordinator.async_write_command(
            self._write_command,
            data,
            recheck_command=self._read_command,
        )
        self.async_write_ha_state()


class JudoHolidayDaysNumber(
    CoordinatorEntity[JudoCoordinator], NumberEntity, RestoreEntity
):
    """Stored UI value used by the separate holiday-mode start button."""

    _attr_has_entity_name = True
    _attr_mode = NumberMode.BOX
    _attr_should_poll = False
    _attr_native_min_value = 1
    _attr_native_max_value = 60
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "Tage"
    _attr_icon = "mdi:beach"

    def __init__(self, coordinator: JudoCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_name = "Urlaubstage"
        self._attr_unique_id = f"{DOMAIN}_holiday_days_{entry.entry_id}"
        self._attr_default_entity_id = (
            f"number.{entity_prefix(coordinator.api.host, coordinator.api.port)}_urlaubstage"
        )
        self._value = coordinator.holiday_days
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": coordinator.device_name,
            "manufacturer": "JUDO Wasseraufbereitung",
        }

    @property
    def available(self) -> bool:
        # This is a local UI setting and must remain editable even when the
        # JUDO device is temporarily offline.
        return True

    @property
    def native_value(self) -> int:
        return self._value

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None:
            try:
                restored = int(float(last_state.state))
            except (TypeError, ValueError):
                restored = self._value
            self._value = max(1, min(60, restored))
        self.coordinator.set_holiday_days(self._value)

    async def async_set_native_value(self, value: float) -> None:
        self._value = max(1, min(60, int(round(value))))
        self.coordinator.set_holiday_days(self._value)
        self.async_write_ha_state()
