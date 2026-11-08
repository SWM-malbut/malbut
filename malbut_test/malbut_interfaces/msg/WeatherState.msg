# Weather data from the named source; valid_at is the current observation time.
builtin_interfaces/Time fetched_at
builtin_interfaces/Time valid_at
string location
float64 latitude
float64 longitude
string source
string timezone
float64 temperature_c
uint16 weather_code
malbut_interfaces/WeatherForecast[] daily
