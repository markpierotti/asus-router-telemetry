# Next Hermes phase: foreground monitoring trial

Use the reverse-engineer profile. The existing DeepSeek Flash / Medium setup is sufficient; this phase needs no new model integration.

```text
Prepare the next phase: a short foreground monitoring trial on my ASUS RT-AX56U using the existing rt-ax56u-telemetry bench project.

First read the current checkpoint and saved evidence. Two real one-shot collections have succeeded: password input, watchdog self-test, verified reporter transfer, accepted JSON and recorded cleanup. The second reported CPU state ok and 1.62% utilisation. An independent calculation from the saved aggregate counters matched: total delta 58587, idle delta 57637, uptime interval approximately 146.4 seconds.

The portable portfolio repository now contains the reporter, collector, tests and curated hardware records. Keep the working bench configuration and private evidence separate from that repository. Use the existing bench project for this trial.

1. Record the two successful hardware collections and the independent CPU calculation in the checkpoint/report. Distinguish this evidence from offline tests and from continuous monitoring, which remains unproven.
2. Inspect the current loop and CLI. Prepare an approximately ten-minute foreground trial at a 60-second interval, using the existing approved authentication and private data directory. Give me the exact command to run in my Terminal. Do not start it yourself. If there is no automatic duration option, give clear manual Ctrl-C instructions; do not add a feature just for this trial.
3. Define acceptance: repeated accepted samples with advancing uptime, valid CPU deltas, no overlapping runs, no collection incident during healthy operation, clean Ctrl-C shutdown, successful temporary-directory cleanup, released local lock and a successful subsequent one-shot collection.
4. After I supply the trial output, inspect the saved local evidence and report sample count, outcomes, collection durations, gaps and CPU values. Treat any failure or cleanup uncertainty explicitly. Do not claim a router outage from an SSH failure alone or claim CPU overhead has been benchmarked by this trial.
5. Healthy collection must make zero model calls. Do not rerun completed offline suites unless code changes or a failure justifies it.

Keep this phase to healthy repeated collection and shutdown. No deliberate disconnects, network/firmware changes, SSH-key installation, persistent router files, services, schedules, USB operations, dashboards, notifications or automatic remediation.

Stop after recording the milestone and providing the concrete trial instructions. Wait for my trial results before evaluating hardware acceptance.
```
