# Python SDK

Install the SDK with `pip install tidewatch`. Python 3.9 or newer is required.

## Quick Start

```python
from tidewatch import Client

client = Client(api_key="tw_test_abc123")
for station in client.stations.list(region="baltic"):
    print(station.id, station.battery_percent)
```

## Retries

The SDK retries 429, 500 and 503 responses automatically up to 5 times with exponential
backoff. Configure it with `Client(max_retries=...)`; set it to 0 to disable retries.

## Async Support

Use `AsyncClient` for asyncio applications. It has the same methods as `Client` but every
call must be awaited.

## Logging

Set the environment variable `TIDEWATCH_LOG=debug` to log every request and response
(API keys are redacted).
