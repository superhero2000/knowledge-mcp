# Tidewatch API – Authentication

The Tidewatch API provides tide, wave and water-temperature data from the Brightwater
buoy network. All requests must be authenticated.

## API Keys

Create an API key in the developer console under "Settings > API keys". Keys start with
the prefix `tw_live_` for production and `tw_test_` for the sandbox. Send the key in the
`X-Tidewatch-Key` header:

```
curl -H "X-Tidewatch-Key: tw_test_abc123" https://api.tidewatch.example/v2/stations
```

Keys never expire, but you can revoke them at any time. Revoked keys stop working within
60 seconds.

## OAuth 2.0 for Partner Apps

Partner applications that act on behalf of users use the OAuth 2.0 authorization code flow
with PKCE. Access tokens are valid for 1 hour, refresh tokens for 30 days. The token
endpoint is `https://auth.tidewatch.example/oauth/token`.

### Scopes

- `stations:read` – list stations and metadata
- `forecasts:read` – read forecasts
- `alerts:write` – create and delete threshold alerts

## Sandbox

The sandbox at `https://sandbox.tidewatch.example/v2` returns synthetic data and is free.
Sandbox keys are limited to 1,000 requests per day.
