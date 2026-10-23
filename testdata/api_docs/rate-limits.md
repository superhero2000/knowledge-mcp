# Rate Limits

Rate limits protect the buoy network backend. Limits apply per API key.

## Plans

| Plan       | Requests per minute | Requests per day | Burst |
|------------|--------------------:|-----------------:|------:|
| Free       | 60                  | 5,000            | 10    |
| Pro        | 600                 | 200,000          | 100   |
| Enterprise | 3,000               | unlimited        | 500   |

## Headers

Every response includes the headers `X-RateLimit-Limit`, `X-RateLimit-Remaining` and
`X-RateLimit-Reset` (Unix timestamp when the window resets).

## Exceeding the Limit

When you exceed the limit, the API answers with HTTP 429 and the error code `TW-429-RATE`.
The `Retry-After` header tells you how many seconds to wait. Clients should back off
exponentially starting at 1 second, up to a maximum of 64 seconds.

Repeatedly ignoring 429 responses for more than 10 minutes leads to a temporary block of
the key for one hour (error code `TW-403-BLOCKED`).
