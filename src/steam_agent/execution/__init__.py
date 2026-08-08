"""Execution broker (ADR 0027, re-scoped by ADR 0028).

This package is the ONLY code in steam-agent that may mutate Steam state,
and it is reachable only through the ``steam-agent-broker`` entry point,
driven directly by the trusted manager agent.  The planner surface
(``steam-agent``) remains inert exactly as ADR 0013 specified.

Scope: install/update of owned titles via steamcmd with single-manifest
adoption, behind a policy-gated authorization ledger (ADR 0028).  Every
other operation class is denied here regardless of policy file content.
"""
