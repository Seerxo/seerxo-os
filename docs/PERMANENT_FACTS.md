# PERMANENT FACTS

Settled facts about your organisation that agents must not re-ask or re-decide. The Founder Desk shows
this file in its Memory view (`GET /desk/memory`), and it is part of the release payload.

Replace the examples below with your own. Never put a secret, an address or personal data here:
record *which* provider or service is used, not how to reach it.

## FACT-01 · Payments provider

- **Fact:** subscriptions are billed through one payment provider.
- **Consequence for agents:** read revenue only from that provider's metrics collector; never estimate it.

## FACT-02 · Deploy platform

- **Fact:** server-side applications are deployed by one platform on one host.
- **Consequence for agents:** a production change goes through the release gateway; agents never deploy.

## FACT-03 · Alerting channel

- **Fact:** alerts go to one chat channel; empty credentials disable delivery but events are still recorded.
- **Consequence for agents:** do not add a second alerting path without approval.
