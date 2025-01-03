---
title: Dagster+ rate limits
---

Dagster+ enforces several rate limits to smoothly distribute the load. Deployments are limited to:

- 40,000 user log events (e.g, `context.log.info`) per minute. This limit only applies to custom logs; system events like the ones that drive orchestration or materialize assets are not subject to this limit.
- 35MB of events per minute. This limit applies to both custom events and system events.

Rate-limited requests return a "429 - Too Many Requests" response. Dagster+ agents automatically retry these requests.

{/* Switching from [Structured event logs](/concepts/logging#structured-event-logs) to [Raw compute logs](/concepts/logging#raw-compute-logs) or reducing your custom log volume can help you stay within these limits. */}
Switching from [Structured event logs](/todo) to [Raw compute logs](/todo) or reducing your custom log volume can help you stay within these limits.