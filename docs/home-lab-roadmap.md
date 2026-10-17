# Home Lab & Security Operations

I’m building a home networking and security lab to investigate how devices behave,
how failures become visible, and how segmentation contains unwanted traffic.
ASUS Router Telemetry has supervised hardware trials and automated regression
tests. Slim now runs unattended collection, DNS filtering and monitoring. Network
detection and segmentation exercises are later stages.

This roadmap states intentions. Each new role requires its own scope, implementation
and validation before it is described as working.

## Components and evidence

| Component | Purpose | Current status |
| --- | --- | --- |
| ASUS Router Telemetry | Device-health collection, management-link failure evidence and cleanup accountability | Experimental v0.1.0 released with collector 1.5.5; collector 1.6.0 key authentication added on this branch |
| Slim Linux host | External collection, DNS and service monitoring | Commissioned 17 October; eight services active at operator check, recent one-minute collection |
| Pi-hole and monitoring | DNS filtering; Prometheus/Grafana dashboards and alert evaluation | Running; custom exporters and 13 rules commissioned; Telegram delivery pending |
| Backup and recovery | Local archives and off-host copies | Manual archives and one Mac copy observed; first scheduled cycle and restore rehearsal pending |
| Hermes | AI-assisted investigation on the Linux host | Planned; not installed as part of this deployment |
| ASUS USB NAS | Shared USB storage | Planned; AP mode is in use, storage performance unvalidated |
| Project Limbo | Simulated noisy IoT traffic, segmentation, visibility and containment measurements | Planned; no completed lab results |
| Security-operations exercises | Detection rules, alert triage, investigation records and response playbooks | Planned; no deployed SOC claimed |

Other previously discussed candidates are Unbound, NetAlertX, router syslog intake,
Loki, Suricata/Zeek and an external availability checker. They are not installed by
this milestone. Assess overlap with existing monitoring and measure host resources
before adding services. Packet inspection needs a verified traffic capture path;
a mirrored switch port and separate sensor NIC are possibilities, not current
inventory. VLAN isolation also needs a suitable routing/firewall boundary.

## What the router milestone establishes

The [case study](case-study.md) documents the software, review-driven corrections
and physical bench work. The current [healthy-path trial](healthy-acceptance-20261014.md)
recorded 14 successful samples. The separate [dropout/recovery trial](dropout-recovery-20261014.md)
recorded 18 attempts: 12 successes and six expected unreachable outcomes, with
incident transitions and cleanup reconciliation.

Collection-incident thresholds identify missing telemetry. They do not detect
intrusions. Device-health telemetry does not provide packet visibility.

## Next validation sequence

1. Complete fresh DNS/probe/scrape and alert-state checks; connect Telegram and
   test delivery. Add an external check for a complete Slim outage.
2. Confirm a scheduled nightly backup and subsequent Mac pull, rehearse a restore,
   and validate controlled host/router restarts, power recovery and longer operation.
3. Install Hermes if still wanted, then plan USB storage and an isolated network
   lab. Establish where traffic can
   be observed before collecting captures or making visibility claims.
4. Run bounded simulated IoT traffic and compare shared-segment behavior with
   isolation and detection. Record packet/byte counts, capture loss, delivery
   across boundaries and alert timing under the documented conditions.
5. Add detections and investigation/response exercises, preserving both successful
   outcomes and failures.

The [dated deployment record](slim-deployment-20261017.md),
[sanitized source examples](../deploy/slim/README.md) and
[operations runbook](slim-operations.md) describe the current host. Earlier bench
acceptance remains attached to its original versions.

Project Limbo’s visual theme can make the topology memorable, while its technical
view must show the actual trust boundaries and measurements. Additional VLANs
alone do not establish stronger security or protect shared Wi-Fi airtime.

## Releases and portfolio presentation

Separate components can retain their own repositories, releases and evidence,
linked from the portfolio. The roadmap does not itself establish deployment or
acceptance of a planned component.
The [release policy](releases.md) explains when a Linux-host integration could
become a new project milestone without changing an unchanged reporter version.

A SOC claim requires demonstrated detection and investigation workflows. Until
those exist, the public description is a home networking and security lab in
development, with one released experimental telemetry component.
