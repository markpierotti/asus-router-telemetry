# Release and component versions

The project release identifies a complete bundle of reporter, collector,
documentation and validation records. Component versions identify changes to
those individual programs. A new project release does not automatically change
both component versions.

The initial public milestone is [v0.1.0](https://github.com/markpierotti/asus-router-telemetry/releases/tag/v0.1.0),
including collector 1.5.5, reporter 1.0.5 and JSON schema router_health/2. The
project remains experimental despite the historical component version numbers.

For future project tags, use 0.MINOR.PATCH until the project declares a stable
supported contract. Increase MINOR for a new deployment role or substantial
feature; increase PATCH for compatible fixes to the existing milestone. Record
breaking changes explicitly rather than assuming pre-1.0 compatibility. Revisit
the policy before declaring project 1.0.0.

A Slim-PC deployment package, service or alerting integration could become project
v0.2.0 while retaining reporter 1.0.5 if the reporter source is unchanged. Merely
confirming the same code on another host adds validation evidence; it need not
change either component version. Changes to the collector or reporter update
that component version and its test record. A schema-contract change must also
identify the schema revision and migration requirements.

Each release should record:

- Project tag and exact Git commit.
- Collector, reporter and schema versions.
- Build/artifact identities when binaries are provided.
- Supported configurations, actual acceptance evidence and remaining limitations.
- Changes and migration instructions relevant to users.

Hardware evidence belongs to its exact artifact and configuration. A documentation
update or commit-metadata rewrite does not create new hardware acceptance. No
release number establishes production reliability by itself.
