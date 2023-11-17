"""Sun2 Helpers."""
from __future__ import annotations

from abc import abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any, TypeVar, Union, cast

from astral import LocationInfo
from astral.location import Location
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_ELEVATION,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_TIME_ZONE,
)
from homeassistant.core import CALLBACK_TYPE
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.device_registry import DeviceEntryType

# Device Info moved to device_registry in 2023.9
try:
    from homeassistant.helpers.device_registry import DeviceInfo
except ImportError:
    from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_NEXT_CHANGE,
    ATTR_TODAY_HMS,
    ATTR_TOMORROW,
    ATTR_TOMORROW_HMS,
    ATTR_YESTERDAY,
    ATTR_YESTERDAY_HMS,
    DOMAIN,
    ONE_DAY,
    SIG_HA_LOC_UPDATED,
)


Num = Union[float, int]
LOC_PARAMS = {
    vol.Inclusive(CONF_ELEVATION, "location"): vol.Coerce(float),
    vol.Inclusive(CONF_LATITUDE, "location"): cv.latitude,
    vol.Inclusive(CONF_LONGITUDE, "location"): cv.longitude,
    vol.Inclusive(CONF_TIME_ZONE, "location"): cv.time_zone,
}


@dataclass(frozen=True)
class LocParams:
    """Location parameters."""

    elevation: Num
    latitude: float
    longitude: float
    time_zone: str


@dataclass(frozen=True)
class LocData:
    """Location data."""

    loc: Location
    elv: Num
    tzi: tzinfo

    def __init__(self, lp: LocParams) -> None:
        """Initialize location data from location parameters."""
        loc = Location(LocationInfo("", "", lp.time_zone, lp.latitude, lp.longitude))
        object.__setattr__(self, "loc", loc)
        object.__setattr__(self, "elv", lp.elevation)
        object.__setattr__(self, "tzi", dt_util.get_time_zone(lp.time_zone))


@dataclass
class Sun2Data:
    """Sun2 shared data."""

    locations: dict[LocParams | None, LocData] = field(default_factory=dict)
    translations: dict[str, str] = field(default_factory=dict)


def get_loc_params(config: ConfigType) -> LocParams | None:
    """Get location parameters from configuration."""
    try:
        return LocParams(
            config[CONF_ELEVATION],
            config[CONF_LATITUDE],
            config[CONF_LONGITUDE],
            config[CONF_TIME_ZONE],
        )
    except KeyError:
        return None


def hours_to_hms(hours: Num | None) -> str | None:
    """Convert hours to HH:MM:SS string."""
    try:
        return str(timedelta(hours=cast(Num, hours))).split(".")[0]
    except TypeError:
        return None


_Num = TypeVar("_Num", bound=Num)


def nearest_second(dttm: datetime) -> datetime:
    """Round dttm to nearest second."""
    return dttm.replace(microsecond=0) + timedelta(
        seconds=0 if dttm.microsecond < 500000 else 1
    )


def next_midnight(dttm: datetime) -> datetime:
    """Return next midnight in same time zone."""
    return datetime.combine(dttm.date() + ONE_DAY, time(), dttm.tzinfo)


class Sun2Entity(Entity):
    """Sun2 Entity."""

    _unreported_attributes = frozenset(
        {
            ATTR_NEXT_CHANGE,
            ATTR_TODAY_HMS,
            ATTR_TOMORROW,
            ATTR_TOMORROW_HMS,
            ATTR_YESTERDAY,
            ATTR_YESTERDAY_HMS,
        }
    )
    _attr_should_poll = False
    _loc_data: LocData = None  # type: ignore[assignment]
    _unsub_update: CALLBACK_TYPE | None = None
    _event: str
    _solar_depression: Num | str

    @abstractmethod
    def __init__(
        self,
        loc_params: LocParams | None,
        entry: ConfigEntry | None,
        unique_id: str | None = None,
    ) -> None:
        """Initialize base class.

        self.name must be set up to return name before calling this.
        E.g., set up self.entity_description.name first.
        """
        if entry:
            self._attr_translation_key = self.entity_description.key
            self._attr_has_entity_name = True
            self._attr_device_info = DeviceInfo(
                entry_type=DeviceEntryType.SERVICE,
                identifiers={(DOMAIN, entry.entry_id)},
                name=entry.title,
            )
            self._attr_unique_id = (
                f"{entry.unique_id}-{unique_id or self.entity_description.key}"
            )
        else:
            self._attr_unique_id = self.name
        self._loc_params = loc_params
        self.async_on_remove(self._cancel_update)

    @property
    def _sun2_data(self) -> Sun2Data:
        return cast(Sun2Data, self.hass.data[DOMAIN])

    async def async_update(self) -> None:
        """Update state."""
        if not self._loc_data:
            self._loc_data = self._get_loc_data()
        self._update(dt_util.now(self._loc_data.tzi))

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added to hass."""
        self._setup_fixed_updating()

    def _cancel_update(self) -> None:
        """Cancel update."""
        if self._unsub_update:
            self._unsub_update()
            self._unsub_update = None

    def _get_loc_data(self) -> LocData:
        """Get location data from location parameters.

        loc_params = None -> Use location parameters from HA's config.
        """
        try:
            loc_data = self._sun2_data.locations[self._loc_params]
        except KeyError:
            loc_data = self._sun2_data.locations[self._loc_params] = LocData(
                cast(LocParams, self._loc_params)
            )

        if not self._loc_params:

            async def loc_updated(loc_data: LocData) -> None:
                """Location updated."""
                await self.async_request_call(self._async_loc_updated(loc_data))

            self.async_on_remove(
                async_dispatcher_connect(self.hass, SIG_HA_LOC_UPDATED, loc_updated)
            )

        return loc_data

    async def _async_loc_updated(self, loc_data: LocData) -> None:
        """Location updated."""
        self._cancel_update()
        self._loc_data = loc_data
        self._setup_fixed_updating()
        self.async_schedule_update_ha_state(True)

    @abstractmethod
    def _update(self, cur_dttm: datetime) -> None:
        """Update state."""
        pass

    def _setup_fixed_updating(self) -> None:
        """Set up fixed updating."""
        pass

    def _astral_event(
        self,
        date_or_dttm: date | datetime,
        event: str | None = None,
        /,
        **kwargs: Mapping[str, Any],
    ) -> Any:
        """Return astral event result."""
        if not event:
            event = self._event
        loc = self._loc_data.loc
        if hasattr(self, "_solar_depression"):
            loc.solar_depression = self._solar_depression
        try:
            if event in ("solar_midnight", "solar_noon"):
                return getattr(loc, event.split("_")[1])(date_or_dttm)
            elif event == "time_at_elevation":
                return loc.time_at_elevation(
                    kwargs["elevation"], date_or_dttm, kwargs["direction"]
                )
            else:
                return getattr(loc, event)(
                    date_or_dttm, observer_elevation=self._loc_data.elv
                )
        except (TypeError, ValueError):
            return None
