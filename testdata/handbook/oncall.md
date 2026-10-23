# On-Call

## Rotation

Each team runs a weekly rotation that hands over every Monday at 11:00 CET.
A rotation has a primary and a secondary engineer. The secondary is paged if the primary
does not acknowledge within 10 minutes.

## Compensation

On-call engineers receive a flat allowance of 250 euros per week of primary duty and
120 euros per week of secondary duty. Time spent handling pages outside working hours
is additionally compensated as overtime at 150%.

## Paging Tool

Pages are delivered by the "Foghorn" paging app. Install it on your phone and test it with
`bw oncall test-page` before your first shift. Acknowledge pages within 5 minutes.

## Handover

At handover, the outgoing engineer writes a summary of open issues, noisy alerts and
ongoing incidents in the team's on-call log. Noisy alerts that fired more than three
times without action must be tuned or deleted within the next week.

## Health and Limits

Nobody is on primary duty for more than one week in four. After a night with more than
two pages, the engineer may start work late the next day without asking.
