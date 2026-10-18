# Cloud cassettes

**Every cassette in this directory is synthetic.** None was recorded from the reMarkable
cloud. Each one is the HTTP traffic between `CloudClient` and `tests/cloud/fake_cloud.py`,
an in-memory sync 1.5 service, while `SyncOnce` runs one of the scenarios in
`tests/cloud/scenarios.py`. Every file blob is a byte-for-byte file of the S3 fixture
notebook (`tests/fixtures/move-test-notebook/`) and is stored as a reference to that file,
not inline. Hosts are `cloud.example.com`, Authorization values are `[scrubbed]`, and the
user token body is the placeholder `scrubbed-user-token`.

Regenerate after changing a scenario, the fake or the client's request pattern:

    uv run python -m tests.cloud.build_cassettes

`tests/cloud/test_cassettes.py` fails if the committed files differ from what the builder
produces, and re-runs the recorder's checks on every file: no Authorization value, nothing
shaped like a bearer token or JWT, no UUID outside `fixture.json`, no inline blob, no host
other than `cloud.example.com`.

Format (`"format": 1`): `interactions` is a list of `{request: {method, url, headers},
response: {status, headers, body | fixture}}` or `{request, error: "connect" |
"read_timeout"}`. Playback (`tests/cloud/cassettes.py:Player`, an `httpx.MockTransport`)
answers each request with the next recorded response for the same method and URL; the last
one is sticky.

Live recording (`Recorder.live`) exists for the operator's scratch account only. It needs
`SCRIPTORIUM_LIVE_TEST=1` and a device token file outside the repository, and its output
passes through the same checks, so it can only produce a cassette of the fixture
documents. Agents never run it.
