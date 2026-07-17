<!-- Keep this short. The issue holds the analysis; the PR holds the change. -->

## What & why
<!-- One or two lines. Link the issue it closes. -->
Closes #

## Change type
- [ ] Bug fix
- [ ] Enhancement
- [ ] Analytics / research (shadow — does not gate execution)
- [ ] Config / infra
- [ ] Docs

## Touches the trading path?
<!-- The executor/monitor/planner/risk/guard place and manage real orders. -->
- [ ] **No** — analysis/UI/docs only
- [ ] **Yes** — explain the capital-risk impact and how it was validated:

## Validation
- [ ] `pytest packages/core/tests services/executor/tests -q` passes (CI runs this)
- [ ] New/changed behaviour has a test
- [ ] Verified against real data (dump / logs) — paste evidence:

## Config impact
<!-- Does this need a settings/sources/accounts change to take effect?
     Anything an operator must set (and the safe default if unset)? -->
- [ ] None
- [ ] Requires config: <!-- key + value + safe default -->

## Rollback
<!-- How to revert safely: flag to flip, setting to unset, or plain revert. -->
