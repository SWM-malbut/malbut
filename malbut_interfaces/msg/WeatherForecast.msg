# Forecast for a local ISO calendar date, YYYY-MM-DD.
string date
float64 temperature_max_c
float64 temperature_min_c
float64 precipitation_probability_max_pct
# WMO code for the most severe condition on this day.
uint16 weather_code
# Paired 24-hour forecasts, both empty when unavailable. Index h represents
# [h:00, (h+1):00) on date in the WeatherState timezone, including 23:00-24:00.
uint8[<=24] hourly_weather_codes
float64[<=24] hourly_precipitation_probability_pct
