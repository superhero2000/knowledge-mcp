# Endpoint: Forecasts

## Get a Forecast

`GET /v2/forecasts?station_id=BW-0042&hours=48`

Returns hourly forecasts for wave height (metres), wave period (seconds), water
temperature (°C) and tide level relative to chart datum. `hours` can be 1 to 168
(seven days). Forecasts are recomputed every three hours at 00:00, 03:00, 06:00 … UTC.

## Accuracy

Wave height forecasts have a mean absolute error of 0.18 m for the next 24 hours and
0.41 m for 72 hours ahead, measured against buoy readings in 2025.

## Threshold Alerts

`POST /v2/alerts` creates an alert that sends a webhook when a forecast crosses a
threshold, for example wave height above 3 m. Requires the `alerts:write` scope.
Each account can have at most 50 active alerts. Webhooks are signed with HMAC-SHA256
in the `X-Tidewatch-Signature` header.
