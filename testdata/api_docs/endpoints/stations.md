# Endpoint: Stations

## List Stations

`GET /v2/stations`

Returns all active buoy stations. Query parameters:

- `region` – filter by region code, for example `north-sea` or `baltic`
- `status` – `active` (default), `maintenance` or `all`
- `page_size` – 1 to 200, default 50
- `cursor` – pagination cursor from the previous response's `next_cursor`

## Get One Station

`GET /v2/stations/{station_id}`

Station IDs look like `BW-0042`. The response includes coordinates, deployment date,
battery level in percent and the list of sensors.

## Station Status Values

A station is in `maintenance` when its battery is below 15% or a technician has scheduled
a service visit. Stations in maintenance still return historical data but no live readings.
