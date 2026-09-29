"""Zaptec Charger implementation - hardened v3 for Issue #73.

Strategy change vs earlier patches:
- Do NOT depend on device_id / entity_registry lookups at all for state reads.
  This host has a STABLE entity_id (sensor.skallbygo_charger_mode) with no serial
  suffix, so we read it directly. This removes every cache/binding that could go
  stale and require a full reinstall.
- All state reads happen live via hass.states.get() on each coordinator tick.
- Never raise from state-reading methods; degrade to safe defaults so the
  coordinator loop can never die on a transient Zaptec 'unavailable'.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.helpers import entity_registry as er

from ..const import CHARGER_DOMAIN_ZAPTEC, Phase  # noqa: TID252
from ..ha_device import HaDevice  # noqa: TID252
from .charger import Charger, PhaseMode

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.device_registry import DeviceEntry

_LOGGER = logging.getLogger(__name__)

# --- Stable, hardcoded entities for this installation --------------------------
# These entity_ids are stable on this host (no serial-number suffix). If you ever
# rename them in HA, update these constants and restart.
ENTITY_OPERATION_MODE: Final = "sensor.skallbygo_charger_mode"
# Optional: set to the real entity_ids if present, else leave as None to fall back
# to device-registry resolution / defaults.
ENTITY_MAX_CURRENT: Final = "number.skallbygo_charger_max_current"
ENTITY_AVAILABLE_CURRENT_L1: Final = None
ENTITY_AVAILABLE_CURRENT_L2: Final = None
ENTITY_AVAILABLE_CURRENT_L3: Final = None

ZAPTEC_SERVICE_DOMAIN: Final = "zaptec"
ZAPTEC_SERVICE_LIMIT_CURRENT: Final = "limit_current"

_TRANSIENT: Final = {STATE_UNAVAILABLE, STATE_UNKNOWN, None, "", "unknown"}


class ZaptecStatusMap:
    """Accepted charger_operation_mode strings (v0.8.x lowercase + legacy)."""

    CONNECTED_REQUESTING = {"connected_requesting", "Connected_Requesting"}
    CONNECTED_CHARGING = {
        "connected_charging",
        "Connected_Charging",
        "charging",
        "Charging",
    }
    CONNECTED_FINISHED = {"connected_finished", "Connected_Finished"}
    CONNECTED_ANY = CONNECTED_REQUESTING | CONNECTED_CHARGING | CONNECTED_FINISHED


class ZaptecCharger(HaDevice, Charger):
    """Zaptec driver that reads stable entity_ids directly (no stale bindings)."""

    _DEFAULT_MAX_CURRENT_AMPS: Final = 16

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        device_entry: DeviceEntry,
    ) -> None:
        """Initialize; device_entry kept only for base-class compatibility."""
        HaDevice.__init__(self, hass, device_entry)
        Charger.__init__(self, hass, config_entry, device_entry)
        # We do not cache entity references; refresh_entities is harmless.
        try:
            self.refresh_entities()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("refresh_entities failed; continuing with hardcoded ids")
        _LOGGER.debug(
            "ZaptecCharger v3 init; op_mode entity=%s", ENTITY_OPERATION_MODE
        )

    @staticmethod
    def is_charger_device(device_entry: DeviceEntry) -> bool:
        """Return True if the HA device belongs to the Zaptec integration."""
        try:
            return any(
                ident and ident[0] == CHARGER_DOMAIN_ZAPTEC
                for ident in device_entry.identifiers
            )
        except (AttributeError, TypeError, IndexError):
            return False

    # --- Live state helpers ----------------------------------------------------
    def _read_state(self, entity_id: str | None) -> str | None:
        """Read a live entity state, tolerating transient/unavailable values."""
        if not entity_id:
            return None
        state_obj = self.hass.states.get(entity_id)
        if state_obj is None:
            _LOGGER.debug("Entity %s not found in state machine", entity_id)
            return None
        value = state_obj.state
        if value in _TRANSIENT:
            _LOGGER.debug("Entity %s transient state '%s'", entity_id, value)
            return None
        return value

    def _read_numeric(self, entity_id: str | None) -> float | None:
        """Read a numeric state safely."""
        raw = self._read_state(entity_id)
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            _LOGGER.debug("Non-numeric '%s' from %s", raw, entity_id)
            return None

    def _operation_mode(self) -> str | None:
        """Return current charger_operation_mode from the stable entity."""
        return self._read_state(ENTITY_OPERATION_MODE)

    # --- Charger ABC: state ----------------------------------------------------
    def car_connected(self) -> bool:
        """True if a car is plugged in (connected in any sub-state)."""
        state = self._operation_mode()
        result = state in ZaptecStatusMap.CONNECTED_ANY if state else False
        _LOGGER.debug("car_connected: state='%s' -> %s", state, result)
        return result

    def can_charge(self) -> bool:
        """True if the charger is ready to deliver current."""
        state = self._operation_mode()
        if not state:
            return False
        return state in (
            ZaptecStatusMap.CONNECTED_REQUESTING | ZaptecStatusMap.CONNECTED_CHARGING
        )

    def is_charging(self) -> bool:
        """True if actively charging. Read live every call; never raises."""
        state = self._operation_mode()
        charging = state in ZaptecStatusMap.CONNECTED_CHARGING if state else False
        _LOGGER.debug("is_charging: state='%s' -> %s", state, charging)
        return charging

    def has_synced_phase_limits(self) -> bool:
        """Zaptec applies a single current limit across active phases."""
        return True

    # --- Charger ABC: limits ---------------------------------------------------
    def get_current_limit(self) -> dict[Phase, int] | None:
        """Return the configured charging limit per phase."""
        per_phase: dict[Phase, int] = {}
        for phase, entity in (
            (Phase.L1, ENTITY_AVAILABLE_CURRENT_L1),
            (Phase.L2, ENTITY_AVAILABLE_CURRENT_L2),
            (Phase.L3, ENTITY_AVAILABLE_CURRENT_L3),
        ):
            value = self._read_numeric(entity)
            if value is not None:
                per_phase[phase] = int(round(value))
        if per_phase:
            fill = min(per_phase.values())
            for phase in (Phase.L1, Phase.L2, Phase.L3):
                per_phase.setdefault(phase, fill)
            return per_phase

        max_current = self._read_numeric(ENTITY_MAX_CURRENT)
        if max_current is None:
            return None
        amps = int(round(max_current))
        return {Phase.L1: amps, Phase.L2: amps, Phase.L3: amps}

    def get_max_current_limit(self) -> dict[Phase, int] | None:
        """Return configured maximum current per phase."""
        max_current = self._read_numeric(ENTITY_MAX_CURRENT)
        amps = (
            int(round(max_current))
            if max_current is not None
            else self._DEFAULT_MAX_CURRENT_AMPS
        )
        return {Phase.L1: amps, Phase.L2: amps, Phase.L3: amps}

    async def set_current_limit(self, limit: dict[Phase, int]) -> None:
        """Apply a new current limit via the Zaptec service, with fallback."""
        if not limit:
            return
        amps = max(0, min(int(v) for v in limit.values()))

        try:
            await self.hass.services.async_call(
                ZAPTEC_SERVICE_DOMAIN,
                ZAPTEC_SERVICE_LIMIT_CURRENT,
                {"amps": amps},
                blocking=False,
            )
            _LOGGER.debug("zaptec.limit_current amps=%s", amps)
            return
        except Exception:  # noqa: BLE001
            _LOGGER.exception("limit_current service failed; trying number entity")

        if ENTITY_MAX_CURRENT:
            try:
                await self.hass.services.async_call(
                    "number",
                    "set_value",
                    {"entity_id": ENTITY_MAX_CURRENT, "value": amps},
                    blocking=False,
                )
                _LOGGER.debug("Set %s=%s via number.set_value", ENTITY_MAX_CURRENT, amps)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Failed to set Zaptec limit via number entity")

    def set_phase_mode(self, mode: PhaseMode, _phase: Phase) -> None:
        """No phase switching available for Zaptec via this integration."""
        if mode not in PhaseMode:
            msg = "Invalid PhaseMode for Zaptec set_phase_mode"
            raise ValueError(msg)
        _LOGGER.debug("set_phase_mode(%s) no-op for Zaptec", mode)

    async def async_setup(self) -> None:
        """No listeners; state is polled live each coordinator tick."""
        _LOGGER.debug("ZaptecCharger v3 async_setup (live-poll mode)")

    async def async_unload(self) -> None:
        """Nothing to tear down."""
        _LOGGER.debug("ZaptecCharger v3 async_unload")