metoffice_datahub — UK Met Office DataHub Site Specific Forecast for WeeWX
==========================================================================

This extension fetches hourly site-specific weather forecasts from the UK
Met Office DataHub API and makes them available in WeeWX report templates.

REQUIREMENTS
------------
* WeeWX 5.x
* Python 3.8+
* A Met Office DataHub API account and key
  Register at: https://datahub.metoffice.gov.uk/

INSTALLATION
------------
1. Package the extension:
       zip -r metoffice_datahub.zip metoffice_datahub/

2. Install via weectl:
       weectl extension install metoffice_datahub.zip

3. Edit weewx.conf and set your API key and station coordinates:
       [MetOfficeDatahub]
           api_key = YOUR_API_KEY_HERE
           latitude = 51.5074      # decimal degrees, positive = north
           longitude = -0.1278     # decimal degrees, positive = east

4. To use forecast data in report templates, add the search list
   extension to your report skin:
       [StdReport]
           [[MyReport]]
               search_list_extensions = user.metoffice_datahub.MetOfficeForecastList

5. Restart WeeWX:
       sudo systemctl restart weewx

CONFIGURATION
-------------
[MetOfficeDatahub]
    api_key        - Your DataHub API key (required)
    latitude       - Site latitude in decimal degrees (required)
    longitude      - Site longitude in decimal degrees (required)
    fetch_interval - Seconds between API calls. Default: 10800 (3 hours = 8
                     calls/day). The Met Office DataHub free plan allows 360
                     calls/day, which means a minimum safe interval of 240 s.
                     The service will clamp any value below 240 s and log a
                     warning. The Met Office only updates site-specific
                     forecasts a few times per day, so the 3-hour default is
                     recommended.
    max_hours      - How many hours of forecast to store. Default: 72.
    data_binding   - WeeWX data binding name. Default: metoffice_binding.

TEMPLATE USAGE
--------------
Once the search list extension is registered, templates can access $forecast:

    ## Next 24 hours
    #for $hour in $forecast.hours(24)
    $hour.valid_time_local  $hour.weather_desc
        Temp: ${hour.screen_temperature_c}°C (feels ${hour.feels_like_c}°C)
        Wind: $hour.wind_speed_mph mph $hour.wind_direction_compass
              gusting $hour.wind_gust_mph mph
        Rain: ${hour.prob_of_precip}% chance, rate ${hour.precip_rate_mm_hr} mm/hr
        UV:   $hour.uv_index
    #end for

    ## 3-day day-by-day summary
    #for $day in $forecast.days(3)
    $day.date
        Min: ${day.min_temp_c}°C  Max: ${day.max_temp_c}°C
        Max rain probability: ${day.max_prob_precip}%
        Total rain: ${day.total_precip_mm} mm
    #end for

    ## Just the next forecast period
    Next: $forecast.next.weather_desc at $forecast.next.screen_temperature_c°C

Available ForecastHour properties
----------------------------------
    valid_time_utc            ISO-8601 UTC string, e.g. "2024-01-15T06:00Z"
    valid_time_local          Local datetime string, e.g. "2024-01-15 06:00"
    valid_date_local          Local date string, e.g. "2024-01-15"
    valid_hour_local          Hour of day 0-23 (local)

    screen_temperature_c      Air temperature at screen level, °C
    screen_temperature_f      Air temperature at screen level, °F
    feels_like_c              Feels-like temperature, °C
    feels_like_f              Feels-like temperature, °F
    dewpoint_c                Dew point, °C
    relative_humidity         Relative humidity, %

    wind_speed_ms             Wind speed, m/s
    wind_speed_mph            Wind speed, mph
    wind_speed_kph            Wind speed, km/h
    wind_speed_knots          Wind speed, knots
    wind_gust_ms              Wind gust, m/s
    wind_gust_mph             Wind gust, mph
    wind_direction_degrees    Wind direction, degrees (meteorological)
    wind_direction_compass    Wind direction, 16-point compass string

    pressure_pa               MSLP, Pa
    pressure_hpa              MSLP, hPa
    pressure_inhg             MSLP, inches of mercury

    precip_rate_mm_hr         Precipitation rate, mm/hr
    precip_total_mm           Total precipitation, mm
    snow_total_mm             Total snow, mm
    prob_of_precip            Probability of precipitation, %

    uv_index                  UV index
    visibility_m              Visibility, metres
    visibility_km             Visibility, kilometres
    weather_code              Significant weather code (integer)
    weather_desc              Weather description string

SIGNIFICANT WEATHER CODES
--------------------------
 0  Clear night           11  Drizzle               22  Light snow shower (night)
 1  Sunny day             12  Light rain            23  Light snow shower (day)
 2  Partly cloudy (night) 13  Heavy rain shower (n) 24  Light snow
 3  Partly cloudy (day)   14  Heavy rain shower (d) 25  Heavy snow shower (night)
 5  Mist                  15  Heavy rain            26  Heavy snow shower (day)
 6  Fog                   16  Sleet shower (night)  27  Heavy snow
 7  Cloudy                17  Sleet shower (day)    28  Thunder shower (night)
 8  Overcast              18  Sleet                 29  Thunder shower (day)
 9  Light rain shower (n) 19  Hail shower (night)   30  Thunder
10  Light rain shower (d) 20  Hail shower (day)
-1  Trace rain            21  Hail

API DOCUMENTATION
-----------------
https://datahub.metoffice.gov.uk/docs/f/category/site-specific/type/site-specific/
