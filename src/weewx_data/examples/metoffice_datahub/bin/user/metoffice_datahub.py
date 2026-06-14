# Copyright 2024 WeeWX Contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""weewx service and search list extension for the UK Met Office DataHub
Site Specific hourly forecast API.

--- Installation ---

1. Install the extension:
       weectl extension install metoffice_datahub.zip

2. Configure weewx.conf (see Configuration section below).

3. Restart WeeWX.

--- Configuration ---

Add the following to weewx.conf:

[MetOfficeDatahub]
    api_key = YOUR_API_KEY_HERE
    latitude = 51.5
    longitude = -0.12
    # How often to fetch a new forecast, in seconds.
    # Free plan: 360 calls/day max → minimum interval 240 s.
    # Default 10800 s (3 h) = 8 calls/day — well within the free tier.
    fetch_interval = 10800
    # How many hours of forecast to retain. Default 72.
    max_hours = 72
    data_binding = metoffice_binding

[DataBindings]
    [[metoffice_binding]]
        database = metoffice_sqlite
        manager = weewx.manager.Manager
        table_name = forecast
        schema = user.metoffice_datahub.schema

[Databases]
    [[metoffice_sqlite]]
        database_name = archive/metoffice.sdb
        database_type = SQLite

[Engine]
    [[Services]]
        data_services = ..., user.metoffice_datahub.MetOfficeDatahub

[StdReport]
    [[SomeReport]]
        search_list_extensions = user.metoffice_datahub.MetOfficeForecastList

--- Template Usage ---

Access forecast data in Cheetah templates via $forecast:

    #for $hour in $forecast.hours(24)
        $hour.valid_time_local  $hour.weather_desc  $hour.screen_temperature_c
    #end for

    Next 3-day summary: $forecast.daily_summary(3)

--- API Notes ---

Requires a Met Office DataHub account and API key. The Site Specific
hourly endpoint is:
  https://data.hub.api.metoffice.gov.uk/sitespecific/v0/point/hourly

See https://datahub.metoffice.gov.uk/ for registration and API docs.
"""

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import weewx
import weewx.manager
from weewx.engine import StdService
from weewx.cheetahgenerator import SearchList
from weeutil.weeutil import to_float, to_int

log = logging.getLogger(__name__)

VERSION = "0.1"

# Forecast database schema.
# forecastTime: Unix timestamp of the period the forecast is valid for.
# fetchTime: Unix timestamp of when the data was retrieved from the API.
schema = [
    ('forecastTime',              'INTEGER NOT NULL PRIMARY KEY'),
    ('fetchTime',                 'INTEGER NOT NULL'),
    ('screenTemperature',         'REAL'),     # °C
    ('feelsLikeTemperature',      'REAL'),     # °C
    ('screenDewPointTemperature', 'REAL'),     # °C
    ('screenRelativeHumidity',    'REAL'),     # %
    ('windSpeed10m',              'REAL'),     # m/s
    ('windDirectionFrom10m',      'INTEGER'),  # degrees (meteorological)
    ('windGustSpeed10m',          'REAL'),     # m/s
    ('mslp',                      'INTEGER'),  # Pa
    ('uvIndex',                   'INTEGER'),
    ('significantWeatherCode',    'INTEGER'),
    ('precipitationRate',         'REAL'),     # mm/hr
    ('totalPrecipAmount',         'REAL'),     # mm
    ('totalSnowAmount',           'REAL'),     # mm
    ('probOfPrecipitation',       'INTEGER'),  # %
    ('visibility',                'INTEGER'),  # m
    ('max10mWindGust',            'REAL'),     # m/s  (daily endpoint only)
    ('maxScreenAirTemp',          'REAL'),     # °C   (daily endpoint only)
    ('minScreenAirTemp',          'REAL'),     # °C   (daily endpoint only)
]

# Significant weather code descriptions from Met Office documentation.
WEATHER_CODES = {
    -1: 'Trace rain',
     0: 'Clear night',
     1: 'Sunny day',
     2: 'Partly cloudy (night)',
     3: 'Partly cloudy (day)',
     4: 'Not used',
     5: 'Mist',
     6: 'Fog',
     7: 'Cloudy',
     8: 'Overcast',
     9: 'Light rain shower (night)',
    10: 'Light rain shower (day)',
    11: 'Drizzle',
    12: 'Light rain',
    13: 'Heavy rain shower (night)',
    14: 'Heavy rain shower (day)',
    15: 'Heavy rain',
    16: 'Sleet shower (night)',
    17: 'Sleet shower (day)',
    18: 'Sleet',
    19: 'Hail shower (night)',
    20: 'Hail shower (day)',
    21: 'Hail',
    22: 'Light snow shower (night)',
    23: 'Light snow shower (day)',
    24: 'Light snow',
    25: 'Heavy snow shower (night)',
    26: 'Heavy snow shower (day)',
    27: 'Heavy snow',
    28: 'Thunder shower (night)',
    29: 'Thunder shower (day)',
    30: 'Thunder',
}

# Wind direction names for 16 compass points.
_WIND_DIRS = [
    'N','NNE','NE','ENE','E','ESE','SE','SSE',
    'S','SSW','SW','WSW','W','WNW','NW','NNW',
]

_API_BASE = 'https://data.hub.api.metoffice.gov.uk/sitespecific/v0/point/hourly'


def _compass(degrees):
    """Return a 16-point compass direction string for a bearing in degrees."""
    if degrees is None:
        return None
    idx = int((degrees + 11.25) / 22.5) % 16
    return _WIND_DIRS[idx]


class MetOfficeDatahub(StdService):
    """Periodically fetch hourly forecasts from the Met Office DataHub
    Site Specific API and persist them to a local SQLite database."""

    def __init__(self, engine, config_dict):
        super().__init__(engine, config_dict)

        cfg = config_dict.get('MetOfficeDatahub', {})
        self.api_key = cfg.get('api_key', '').strip()
        if not self.api_key:
            log.error("MetOfficeDatahub: no api_key configured — service disabled")
            return

        self.latitude  = to_float(cfg.get('latitude'))
        self.longitude = to_float(cfg.get('longitude'))
        if self.latitude is None or self.longitude is None:
            log.error("MetOfficeDatahub: latitude/longitude not configured — service disabled")
            return

        # Met Office DataHub free plan: 360 API calls/day = 1 call per 240 s maximum.
        # Default 10800 s (3 h) = 8 calls/day — well within the free tier.
        # Do not set below 240 s on the free plan.
        self.fetch_interval = to_int(cfg.get('fetch_interval', 10800))
        if self.fetch_interval < 240:
            log.warning(
                "MetOfficeDatahub: fetch_interval %d s would exceed the free-plan "
                "limit of 360 calls/day (minimum 240 s). Clamping to 240 s.",
                self.fetch_interval,
            )
            self.fetch_interval = 240
        self.max_hours = to_int(cfg.get('max_hours', 72))

        binding = cfg.get('data_binding', 'metoffice_binding')
        self.dbm = self.engine.db_binder.get_manager(
            data_binding=binding, initialize=True
        )

        self._last_fetch = 0
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._fetch_loop, daemon=True,
                                        name='MetOfficeDatahub')
        self._thread.start()
        log.info("MetOfficeDatahub: service started (fetch every %ds)", self.fetch_interval)

    # ------------------------------------------------------------------
    # Background fetch loop
    # ------------------------------------------------------------------

    def _fetch_loop(self):
        while not self._stop_event.is_set():
            now = time.time()
            if now - self._last_fetch >= self.fetch_interval:
                try:
                    self._fetch_and_store()
                    self._last_fetch = time.time()
                except Exception as e:
                    log.error("MetOfficeDatahub: fetch failed: %s", e)
            self._stop_event.wait(60)

    def _fetch_and_store(self):
        url = (
            f"{_API_BASE}"
            f"?latitude={self.latitude}"
            f"&longitude={self.longitude}"
            f"&includeLocationName=true"
        )
        req = urllib.request.Request(url, headers={
            'apikey': self.api_key,
            'Accept': 'application/json',
        })
        log.debug("MetOfficeDatahub: fetching %s", url)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', errors='replace')
            raise RuntimeError(f"HTTP {e.code}: {body[:200]}") from e

        records = _parse_response(raw)
        fetch_time = int(time.time())
        cutoff = fetch_time + self.max_hours * 3600

        with self.dbm.connection as conn:
            for rec in records:
                if rec['forecastTime'] > cutoff:
                    continue
                rec['fetchTime'] = fetch_time
                cols = [c for c, _ in schema if c in rec]
                placeholders = ', '.join(['?'] * len(cols))
                col_names = ', '.join(cols)
                values = [rec[c] for c in cols]
                conn.execute(
                    f"INSERT OR REPLACE INTO {self.dbm.table_name} ({col_names}) "
                    f"VALUES ({placeholders})",
                    values
                )

        # Prune old rows.
        cutoff_past = fetch_time - 3600
        with self.dbm.connection as conn:
            conn.execute(
                f"DELETE FROM {self.dbm.table_name} WHERE forecastTime < ?",
                (cutoff_past,)
            )

        log.info("MetOfficeDatahub: stored %d forecast hours", len(records))

    def shutDown(self):
        self._stop_event.set()
        self._thread.join(timeout=5)
        self.dbm.close()


# ------------------------------------------------------------------
# JSON parsing
# ------------------------------------------------------------------

def _parse_response(data):
    """Parse the GeoJSON FeatureCollection from the DataHub API.

    Returns a list of dicts suitable for insertion into the schema.
    """
    records = []
    features = data.get('features', [])
    if not features:
        log.warning("MetOfficeDatahub: API response contained no features")
        return records

    props = features[0].get('properties', {})
    time_series = props.get('timeSeries', [])

    for entry in time_series:
        t = entry.get('time', '')
        if not t:
            continue
        # Parse ISO-8601 UTC timestamp, e.g. "2024-01-15T06:00Z"
        try:
            dt = datetime.fromisoformat(t.replace('Z', '+00:00'))
            forecast_time = int(dt.timestamp())
        except ValueError:
            log.warning("MetOfficeDatahub: could not parse time '%s'", t)
            continue

        rec = {
            'forecastTime': forecast_time,
        }
        _copy(rec, entry, 'screenTemperature')
        _copy(rec, entry, 'feelsLikeTemperature')
        _copy(rec, entry, 'screenDewPointTemperature')
        _copy(rec, entry, 'screenRelativeHumidity')
        _copy(rec, entry, 'windSpeed10m')
        _copy(rec, entry, 'windDirectionFrom10m')
        _copy(rec, entry, 'windGustSpeed10m')
        _copy(rec, entry, 'mslp')
        _copy(rec, entry, 'uvIndex')
        _copy(rec, entry, 'significantWeatherCode')
        _copy(rec, entry, 'precipitationRate')
        _copy(rec, entry, 'totalPrecipAmount')
        _copy(rec, entry, 'totalSnowAmount')
        _copy(rec, entry, 'probOfPrecipitation')
        _copy(rec, entry, 'visibility')
        _copy(rec, entry, 'max10mWindGust')
        _copy(rec, entry, 'maxScreenAirTemp')
        _copy(rec, entry, 'minScreenAirTemp')
        records.append(rec)

    return records


def _copy(dest, src, key):
    if key in src and src[key] is not None:
        dest[key] = src[key]


# ------------------------------------------------------------------
# Forecast hour wrapper — used by the SearchList
# ------------------------------------------------------------------

class ForecastHour:
    """Wraps a single forecast row for convenient template access."""

    def __init__(self, row):
        self._row = row

    def _get(self, key):
        return self._row.get(key)

    # --- Times ---

    @property
    def forecast_time(self):
        """Unix timestamp of the forecast valid time."""
        return self._get('forecastTime')

    @property
    def valid_time_utc(self):
        """Forecast valid time as a UTC datetime string (ISO-8601)."""
        t = self._get('forecastTime')
        if t is None:
            return None
        return datetime.fromtimestamp(t, tz=timezone.utc).strftime('%Y-%m-%dT%H:%MZ')

    @property
    def valid_time_local(self):
        """Forecast valid time as a local datetime string."""
        t = self._get('forecastTime')
        if t is None:
            return None
        return datetime.fromtimestamp(t).strftime('%Y-%m-%d %H:%M')

    @property
    def valid_date_local(self):
        """Forecast valid date as a local date string (YYYY-MM-DD)."""
        t = self._get('forecastTime')
        if t is None:
            return None
        return datetime.fromtimestamp(t).strftime('%Y-%m-%d')

    @property
    def valid_hour_local(self):
        """Hour of day (0-23) for the forecast period, in local time."""
        t = self._get('forecastTime')
        if t is None:
            return None
        return datetime.fromtimestamp(t).hour

    # --- Temperature ---

    @property
    def screen_temperature_c(self):
        """Screen-level air temperature in °C."""
        return self._get('screenTemperature')

    @property
    def feels_like_c(self):
        """Feels-like temperature in °C."""
        return self._get('feelsLikeTemperature')

    @property
    def dewpoint_c(self):
        """Dew point temperature in °C."""
        return self._get('screenDewPointTemperature')

    @property
    def screen_temperature_f(self):
        """Screen-level air temperature in °F."""
        c = self._get('screenTemperature')
        return round(c * 9 / 5 + 32, 1) if c is not None else None

    @property
    def feels_like_f(self):
        """Feels-like temperature in °F."""
        c = self._get('feelsLikeTemperature')
        return round(c * 9 / 5 + 32, 1) if c is not None else None

    # --- Humidity ---

    @property
    def relative_humidity(self):
        """Screen relative humidity in %."""
        return self._get('screenRelativeHumidity')

    # --- Wind ---

    @property
    def wind_speed_ms(self):
        """10 m wind speed in m/s."""
        return self._get('windSpeed10m')

    @property
    def wind_speed_mph(self):
        """10 m wind speed in mph."""
        v = self._get('windSpeed10m')
        return round(v * 2.23694, 1) if v is not None else None

    @property
    def wind_speed_kph(self):
        """10 m wind speed in km/h."""
        v = self._get('windSpeed10m')
        return round(v * 3.6, 1) if v is not None else None

    @property
    def wind_speed_knots(self):
        """10 m wind speed in knots."""
        v = self._get('windSpeed10m')
        return round(v * 1.94384, 1) if v is not None else None

    @property
    def wind_gust_ms(self):
        """10 m wind gust speed in m/s."""
        return self._get('windGustSpeed10m')

    @property
    def wind_gust_mph(self):
        """10 m wind gust speed in mph."""
        v = self._get('windGustSpeed10m')
        return round(v * 2.23694, 1) if v is not None else None

    @property
    def wind_direction_degrees(self):
        """Wind direction in degrees (meteorological, from)."""
        return self._get('windDirectionFrom10m')

    @property
    def wind_direction_compass(self):
        """Wind direction as a 16-point compass string, e.g. 'SW'."""
        return _compass(self._get('windDirectionFrom10m'))

    # --- Pressure ---

    @property
    def pressure_pa(self):
        """Mean sea level pressure in Pa."""
        return self._get('mslp')

    @property
    def pressure_hpa(self):
        """Mean sea level pressure in hPa (= mbar)."""
        p = self._get('mslp')
        return round(p / 100, 1) if p is not None else None

    @property
    def pressure_inhg(self):
        """Mean sea level pressure in inches of mercury."""
        p = self._get('mslp')
        return round(p / 3386.39, 2) if p is not None else None

    # --- Precipitation ---

    @property
    def precip_rate_mm_hr(self):
        """Precipitation rate in mm/hr."""
        return self._get('precipitationRate')

    @property
    def precip_total_mm(self):
        """Total precipitation amount in mm."""
        return self._get('totalPrecipAmount')

    @property
    def snow_total_mm(self):
        """Total snow amount in mm."""
        return self._get('totalSnowAmount')

    @property
    def prob_of_precip(self):
        """Probability of precipitation in %."""
        return self._get('probOfPrecipitation')

    # --- Other ---

    @property
    def uv_index(self):
        """UV index (integer)."""
        return self._get('uvIndex')

    @property
    def visibility_m(self):
        """Visibility in metres."""
        return self._get('visibility')

    @property
    def visibility_km(self):
        """Visibility in kilometres."""
        v = self._get('visibility')
        return round(v / 1000, 1) if v is not None else None

    @property
    def weather_code(self):
        """Met Office significant weather code (integer)."""
        return self._get('significantWeatherCode')

    @property
    def weather_desc(self):
        """Human-readable weather description for the significant weather code."""
        code = self._get('significantWeatherCode')
        if code is None:
            return 'Unknown'
        return WEATHER_CODES.get(int(code), f'Code {code}')

    def __repr__(self):
        return f"ForecastHour({self.valid_time_utc}, {self.weather_desc})"


# ------------------------------------------------------------------
# SearchList extension — $forecast in Cheetah templates
# ------------------------------------------------------------------

class MetOfficeForecastList(SearchList):
    """Provides $forecast to Cheetah report templates.

    Register in weewx.conf under [StdReport]:
        search_list_extensions = user.metoffice_datahub.MetOfficeForecastList

    Usage examples:
        #for $hour in $forecast.hours(24)
            $hour.valid_time_local  $hour.weather_desc  $hour.screen_temperature_c°C
        #end for

        Next hour: $forecast.next.weather_desc

        #for $day in $forecast.days(3)
            Date: $day.date
            #for $hour in $day.hours
                $hour.valid_hour_local:00  $hour.weather_desc
            #end for
        #end for
    """

    def __init__(self, generator):
        super().__init__(generator)
        cfg = generator.config_dict.get('MetOfficeDatahub', {})
        binding = cfg.get('data_binding', 'metoffice_binding')
        try:
            self.dbm = generator.db_binder.get_manager(
                data_binding=binding, initialize=False
            )
        except Exception as e:
            log.error("MetOfficeForecastList: cannot open database: %s", e)
            self.dbm = None

    def get_extension_list(self, timespan, db_lookup):
        return [{'forecast': _ForecastAccessor(self.dbm)}]


class _ForecastDay:
    """Groups ForecastHour objects for one calendar day (local time)."""

    def __init__(self, date_str, hours):
        self.date = date_str
        self.hours = hours

    @property
    def min_temp_c(self):
        vals = [h.screen_temperature_c for h in self.hours if h.screen_temperature_c is not None]
        return min(vals) if vals else None

    @property
    def max_temp_c(self):
        vals = [h.screen_temperature_c for h in self.hours if h.screen_temperature_c is not None]
        return max(vals) if vals else None

    @property
    def max_prob_precip(self):
        vals = [h.prob_of_precip for h in self.hours if h.prob_of_precip is not None]
        return max(vals) if vals else None

    @property
    def total_precip_mm(self):
        vals = [h.precip_total_mm for h in self.hours if h.precip_total_mm is not None]
        return sum(vals) if vals else 0.0

    @property
    def max_uv(self):
        vals = [h.uv_index for h in self.hours if h.uv_index is not None]
        return max(vals) if vals else None

    def __repr__(self):
        return f"_ForecastDay({self.date}, {len(self.hours)} hours)"


class _ForecastAccessor:
    """The object bound to $forecast in templates."""

    def __init__(self, dbm):
        self.dbm = dbm

    def _load(self, from_ts=None, limit_hours=None):
        """Return a list of ForecastHour for rows in the database."""
        if self.dbm is None:
            return []
        now = int(time.time())
        cutoff = from_ts if from_ts is not None else now
        col_names = [c for c, _ in schema]
        cols_sql = ', '.join(col_names)
        sql = (
            f"SELECT {cols_sql} FROM {self.dbm.table_name} "
            f"WHERE forecastTime >= ? ORDER BY forecastTime ASC"
        )
        params = [cutoff]
        if limit_hours:
            sql += " LIMIT ?"
            params.append(limit_hours)
        try:
            rows = self.dbm.connection.execute(sql, params).fetchall()
        except Exception as e:
            log.error("MetOfficeForecastList: query failed: %s", e)
            return []
        return [ForecastHour(dict(zip(col_names, row))) for row in rows]

    def hours(self, n=24):
        """Return the next *n* forecast hours as ForecastHour objects."""
        return self._load(limit_hours=n)

    @property
    def next(self):
        """The immediately next forecast hour."""
        hrs = self._load(limit_hours=1)
        return hrs[0] if hrs else None

    def days(self, n=3):
        """Return the next *n* calendar days as _ForecastDay objects."""
        all_hours = self._load(limit_hours=n * 24)
        day_map = {}
        for h in all_hours:
            d = h.valid_date_local
            if d not in day_map:
                day_map[d] = []
            day_map[d].append(h)
        days = [_ForecastDay(d, day_map[d]) for d in sorted(day_map)]
        return days[:n]

    def all(self):
        """Return all stored forecast hours."""
        return self._load()
