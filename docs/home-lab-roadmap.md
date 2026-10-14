# Home Lab & Security Operations

I’m building a home networking and security lab to investigate how devices behave,
how failures become visible, and how segmentation contains unwanted traffic.
ASUS Router Telemetry is the first completed component, with supervised hardware
trials and automated regression tests. Linux-host integration and network detection
exercises are the next stages.

This roadmap states intentions. Each new role requires its own scope, implementation
and validation before it is described as working.

## Components and evidence

| Component | Purpose | Current status |
| --- | --- | --- |
| ASUS Router Telemetry | Device-health collection, management-link failure evidence and cleanup accountability | Experimental v0.1.0 released; supervised hardware trials documented |
| Slim Linux host | External collection and longer-running validation | Next integration milestone; service deployment and unattended authentication remain unvalidated |
| Project Limbo | Simulated noisy IoT traffic, segmentation, visibility and containment measurements | Planned; no completed lab results |
| Security-operations exercises | Detection rules, alert triage, investigation records and response playbooks | Planned; no deployed SOC claimed |

## What the router milestone establishes

The [case study](case-study.md) documents the software, review-driven corrections
and physical bench work. The current [healthy-path trial](healthy-acceptance-20261014.md)
recorded 14 successful samples. The separate [dropout/recovery trial](dropout-recovery-20261014.md)
recorded 18 attempts: 12 successes and six expected unreachable outcomes, with
incident transitions and cleanup reconciliation.

Collection-incident thresholds identify missing telemetry. They do not detect
intrusions. Device-health telemetry does not provide packet visibility.

## Next validation sequence

1. Commission the Linux host and validate the portable collector configuration.
2. Review authentication and any foreground-to-service changes, then validate
   longer operation, interruption and host/router restart behavior.
3. Build an isolated, explicitly scoped network lab. Establish where traffic can
   be observed before collecting captures or making visibility claims.
4. Run bounded simulated IoT traffic and compare shared-segment behavior with
   isolation and detection. Record packet/byte counts, capture loss, delivery
   across boundaries and alert timing under the documented conditions.
5. Add detections and investigation/response exercises, preserving both successful
   outcomes and failures.

Project Limbo’s visual theme can make the topology memorable, while its technical
view must show the actual trust boundaries and measurements. Additional VLANs
alone do not establish stronger security or protect shared Wi-Fi airtime.

## Releases and portfolio presentation

Separate components can retain their own repositories, releases and evidence,
linked from the portfolio. This roadmap creates no service or deployment.
The [release policy](releases.md) explains when a Linux-host integration could
become a new project milestone without changing an unchanged reporter version.

A SOC claim requires demonstrated detection and investigation workflows. Until
those exist, the public description is a home networking and security lab in
development, with one released experimental telemetry component.
