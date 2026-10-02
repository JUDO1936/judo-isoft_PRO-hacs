"""Central serialized coordinator for all JUDO reads and writes."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .api import JudoApi, JudoApiError
from .const import (
    CMD_6900_READ,
    MIN_COMMAND_INTERVAL,
    OFFLINE_AFTER,
    POLL_6900_INTERVAL,
    POLL_COMMANDS,
    POLL_INTERVAL,
    RECHECK_DELAY,
    RECHECKABLE_COMMANDS,
)
from .protocol import Judo6900Status, decode_6900


class JudoCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Poll documented commands sequentially and keep last valid values."""

    def __init__(self, hass: HomeAssistant, api: JudoApi, device_name: str) -> None:
        self.api = api
        self.device_name = device_name.strip() or "JUDO i-soft PRO / PRO L"
        self._cache: dict[str, str] = {}
        self._errors: dict[str, str] = {}
        self._last_success: dict[str, datetime] = {}
        self._selected_scene = "0"
        self._selected_scene_duration = "0100"
        self._recheck_tasks: dict[str, asyncio.Task[None]] = {}
        self._device_seen = False
        self._holiday_days = 1
        self._last_any_success: datetime | None = None
        self._poll_6900_unsub = None

        super().__init__(
            hass,
            logger=logging.getLogger(__name__),
            name=f"JUDO i-soft PRO REST {api.host}:{api.port}",
            update_method=self._async_update_data,
            update_interval=POLL_INTERVAL,
            always_update=True,
        )

    def start_6900_polling(self) -> None:
        """Start the dedicated 6900 polling timer after initial setup succeeds."""
        if self._poll_6900_unsub:
            return
        from homeassistant.helpers.event import async_track_time_interval

        self._poll_6900_unsub = async_track_time_interval(
            self.hass, self._async_poll_6900_timer, POLL_6900_INTERVAL
        )

    @property
    def available(self) -> bool:
        # Keep entities available after a timeout so their last good values stay
        # visible. The dedicated device-status sensor reports Online/Offline.
        return self._device_seen

    @property
    def is_online(self) -> bool:
        if not self._last_any_success:
            return False
        return datetime.now(timezone.utc) - self._last_any_success < OFFLINE_AFTER

    @property
    def last_success(self) -> datetime | None:
        """Timestamp of the most recent successful REST response."""
        return max(self._last_success.values()) if self._last_success else None

    @property
    def selected_scene(self) -> str:
        return self._selected_scene

    @property
    def selected_scene_duration(self) -> str:
        return self._selected_scene_duration

    @property
    def holiday_days(self) -> int:
        return self._holiday_days

    def set_holiday_days(self, value: int) -> None:
        self._holiday_days = max(1, min(60, int(value)))

    @property
    def status_6900(self) -> Judo6900Status | None:
        """Return the currently cached and decoded 6900 status."""
        return decode_6900(self._cache.get(CMD_6900_READ))

    def set_scene_selection(self, scene_code: str, duration_code: str) -> None:
        self._selected_scene = scene_code
        self._selected_scene_duration = duration_code

    def value(self, command: str) -> str | None:
        return self._cache.get(command)

    def error(self, command: str) -> str | None:
        return self._errors.get(command)

    async def _read_command(self, command: str) -> str:
        if command == CMD_6900_READ:
            return await self.api.read(CMD_6900_READ)
        return await self.api.read(command)

    async def _record_success(self, command: str, raw: str) -> None:
        self._cache[command] = raw
        self._errors.pop(command, None)
        timestamp = datetime.now(timezone.utc)
        self._last_success[command] = timestamp
        self._last_any_success = timestamp
        self._device_seen = True

    async def async_poll_6900(self) -> None:
        """Poll the live 6900 status and publish the updated state."""
        try:
            raw = await self._read_command(CMD_6900_READ)
        except JudoApiError as err:
            self._errors[CMD_6900_READ] = str(err)
            self.async_set_updated_data(self._build_data())
            return

        await self._record_success(CMD_6900_READ, raw)
        self.async_set_updated_data(self._build_data())

    async def _async_poll_6900_timer(self, _now: datetime) -> None:
        await self.async_poll_6900()

    async def _async_update_data(self) -> dict[str, Any]:
        changed: list[str] = []

        for command in POLL_COMMANDS:
            old = self._cache.get(command)
            try:
                raw = await self._read_command(command)
            except JudoApiError as err:
                # Keep the last good value. A temporary empty response must
                # never turn an existing sensor value into unavailable.
                self._errors[command] = str(err)
                continue

            await self._record_success(command, raw)

            if old is not None and old != raw and command in RECHECKABLE_COMMANDS:
                changed.append(command)

        for command in changed:
            self.schedule_recheck(command)

        return self._build_data()

    async def async_write_command(
        self,
        command: str,
        data_hex: str = "",
        recheck_command: str | None = None,
    ) -> None:
        """Send a write through the same serialized command queue."""
        try:
            await self.api.write(command, data_hex)
            timestamp = datetime.now(timezone.utc)
            self._last_success[command] = timestamp
            self._last_any_success = timestamp
            self._device_seen = True
            self._errors.pop(command, None)
        except JudoApiError as err:
            self._errors[command] = str(err)
            raise

        self.schedule_recheck(recheck_command or (
            command if command in RECHECKABLE_COMMANDS else None
        ))

    def schedule_recheck(self, command: str | None) -> None:
        if not command:
            return
        existing = self._recheck_tasks.get(command)
        if existing and not existing.done():
            return
        self._recheck_tasks[command] = self.hass.async_create_task(
            self._delayed_recheck(command)
        )

    async def _delayed_recheck(self, command: str) -> None:
        try:
            await asyncio.sleep(RECHECK_DELAY)
            try:
                raw = await self._read_command(command)
            except JudoApiError as err:
                self._errors[command] = str(err)
                return
            await self._record_success(command, raw)
            self.async_set_updated_data(self._build_data())
        finally:
            self._recheck_tasks.pop(command, None)

    def _build_data(self) -> dict[str, Any]:
        return {
            "raw": dict(self._cache),
            "errors": dict(self._errors),
            "last_success": dict(self._last_success),
            "last_any_success": self._last_any_success,
            "online": self.is_online,
            "available": self._device_seen,
            "min_command_interval": MIN_COMMAND_INTERVAL,
        }
