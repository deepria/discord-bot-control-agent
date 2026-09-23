# Deployment status v1

`GET /deployments` reads deploy-produced evidence for the Bot and Control Agent.
It no longer infers a successful deployment from a Git checkout or a systemd
unit that happens to be active. Missing, invalid, incomplete, or expired
evidence is returned as `unknown` or `stale`.

Each CT 101 deploy service must call `scripts/write_deploy_status.py` at the
start of the run, before every major phase, and on both success and failure.
Write records outside the repositories, using the paths configured by
`RIO_BOT_DEPLOY_STATUS_PATH` and `RIO_AGENT_DEPLOY_STATUS_PATH`.

```bash
python3 /opt/rio-agent/scripts/write_deploy_status.py \
  --file /run/rio-agent/deployments/bot.json \
  --component bot \
  --deployment-id "$INVOCATION_ID" \
  --status succeeded --phase readiness \
  --target-revision <full-sha> --running-revision <full-sha> \
  --verified-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --check bot-service=passed --check agent-status=passed --check discord-ready=passed
```

For the Agent, use `--component agent` and checks such as `agent-service` and
`agent-health`. A `succeeded` record must contain identical target/running
revisions, `verified_at`, and at least one passing check. Failure records use a
short content-free error; never include credentials, environment values,
Discord messages, or raw journal output.
