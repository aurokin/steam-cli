"""Provisioned execution broker (ADR 0027, Phase 1).

This package is the ONLY code in steam-agent that may mutate Steam state,
and it is reachable only through the ``steam-agent-broker`` entry point,
which the owner provisions under a dedicated OS identity.  The planner
surface (``steam-agent``) remains inert exactly as ADR 0013 specified.

Phase 1 scope: install/update of owned titles via steamcmd with
single-manifest adoption, behind an explicit-confirmation ledger.  Every
other operation class is denied here regardless of policy file content.
"""
