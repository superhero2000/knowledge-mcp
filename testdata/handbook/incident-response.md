# Incident Response

## Severity Levels

- **SEV1**: complete outage of a customer-facing product or data loss. Page immediately,
  status page update within 15 minutes, executive notification within 30 minutes.
- **SEV2**: major degradation for many customers (for example forecasts delayed by more
  than one hour). Page the on-call engineer; status page update within 30 minutes.
- **SEV3**: minor degradation or an internal tool outage. Handle during business hours.

## Roles

Every SEV1 and SEV2 incident has an **Incident Commander (IC)**, a **Communications Lead**
and one or more **Responders**. The IC coordinates, does not debug, and makes decisions.
The on-call engineer is IC by default until they hand over explicitly.

## Declaring an Incident

Declare an incident in chat with `/incident declare sev2 <short title>`. This creates a
dedicated channel named `inc-<date>-<slug>`, starts the incident timeline and pages the
on-call rotation.

## Communication

Customer-facing updates go to the status page at least every 30 minutes for SEV1 and
every 60 minutes for SEV2, even if there is no news. Internal updates go to the
incident channel. Never share customer names in the status page.

## Postmortems

A blameless postmortem is required for every SEV1 and SEV2 within five business days.
The postmortem template lives in the wiki under "Reliability > Postmortem template".
Action items are tracked in the RELIABILITY project with the label `postmortem`.
