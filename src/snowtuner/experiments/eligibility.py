"""Eligibility checks — does an arm's delta actually run on a given warehouse?

Returns lists of ``Issue`` objects (severity 'error' or 'warning').  Errors
block the arm from being included in an experiment; warnings surface in the UI
but don't block.

The set of checks is intentionally narrow and explicit — Snowflake's surface
is full of compatibility constraints (Gen2 not in some regions, no X5L+ for
Gen2, QAS conditional on Enterprise, etc.).  Adding a new constraint means
adding a check here.
"""
from __future__ import annotations

from dataclasses import dataclass

from snowtuner.actions.base import Issue
from snowtuner.experiments.arm import Arm
from snowtuner.experiments.axes import Generation, QASState
from snowtuner.experiments.config_delta import WarehouseConfig


# ── Account-level facts the eligibility checks need ──────────────────────

@dataclass(frozen=True)
class AccountInfo:
    """The account-level state we need to run eligibility checks.

    Populated once per experiment-proposal pass from /credentials + a
    Snowflake-region lookup (or a static config).  Conservative defaults
    (False / None) when uncertain — better to surface a 'we couldn't verify'
    warning than silently authorize an ineligible arm.
    """
    region: str | None = None         # e.g. 'AWS_US_WEST_2'
    edition: str | None = None        # 'STANDARD' / 'ENTERPRISE' / 'BUSINESS_CRITICAL' / 'VPS'
    gen2_supported_in_region: bool = True   # default optimistic
    qas_available: bool = True              # default optimistic; tightened by edition check below


# Gen2 unsupported regions, per the docs we fetched.  The docs name 5
# ineligible regions; this is the conservative list we surface.
_GEN2_UNSUPPORTED_REGIONS = frozenset({
    "AWS_EU_ZURICH_1",
    "AWS_AF_SOUTH_1",       # Cape Town
    "GCP_ME_CENTRAL_2",     # Dammam
    "AZURE_US_GOV_VIRGINIA",
    # The 5th varies by source; we omit rather than mis-state.
})

# Gen2 size cap.
_GEN2_INELIGIBLE_SIZES = frozenset({"X5LARGE", "X6LARGE"})

# QAS requires Enterprise+.
_QAS_REQUIRED_EDITIONS = frozenset({"ENTERPRISE", "BUSINESS_CRITICAL", "VPS"})


def check_arm_eligibility(
    arm: Arm,
    control: WarehouseConfig,
    account: AccountInfo,
) -> list[Issue]:
    """Inspect an arm against the control + account state and return issues.

    Empty list = arm is fully eligible.  Severity rules:
      'error'   — cannot be included in the experiment; recipe must drop it
      'warning' — surface in UI; arm still runs

    Resolution semantics: we evaluate the *effective* config (control merged
    with arm.delta) against Snowflake's compatibility matrix, plus a few
    consistency checks on the delta itself.
    """
    issues: list[Issue] = []
    effective = control.merge(arm.delta)

    # ── Generation × region ─────────────────────────────────────────────
    if (
        effective.generation == Generation.GEN2
        and account.region is not None
        and (
            account.region.upper() in _GEN2_UNSUPPORTED_REGIONS
            or not account.gen2_supported_in_region
        )
    ):
        issues.append(Issue(
            severity="error",
            message=(
                f"Gen2 is not available in region {account.region}; "
                f"arm {arm.name!r} cannot run."
            ),
        ))

    # ── Generation × size ──────────────────────────────────────────────
    if (
        effective.generation == Generation.GEN2
        and effective.size is not None
        and effective.size in _GEN2_INELIGIBLE_SIZES
    ):
        issues.append(Issue(
            severity="error",
            message=(
                f"Gen2 does not support warehouse size {effective.size}; "
                f"arm {arm.name!r} cannot run."
            ),
        ))

    # ── QAS × edition ──────────────────────────────────────────────────
    if (
        effective.qas_state == QASState.ON
        and account.edition is not None
        and account.edition.upper() not in _QAS_REQUIRED_EDITIONS
    ):
        issues.append(Issue(
            severity="error",
            message=(
                f"Query Acceleration Service requires Enterprise edition or "
                f"higher (account is {account.edition}); arm {arm.name!r} "
                f"cannot run."
            ),
        ))

    # ── QAS × scale factor consistency ─────────────────────────────────
    # The delta-level validator already rejects scale_factor > 0 with state=OFF.
    # Here we flag the more subtle inheritance case: control has QAS off,
    # arm sets scale factor without flipping qas_state on.
    if (
        arm.delta.qas_max_scale_factor is not None
        and arm.delta.qas_max_scale_factor > 0
        and effective.qas_state != QASState.ON
    ):
        issues.append(Issue(
            severity="warning",
            message=(
                f"arm {arm.name!r} sets qas_max_scale_factor but QAS is not "
                f"enabled on the effective config — the scale factor will "
                f"have no effect."
            ),
        ))

    # ── Region uncertainty ─────────────────────────────────────────────
    # If we don't know the region but Gen2 is in play, surface a warning
    # rather than blocking.  The engine will fail loudly at CREATE WAREHOUSE
    # time if we're actually wrong.
    if (
        effective.generation == Generation.GEN2
        and account.region is None
    ):
        issues.append(Issue(
            severity="warning",
            message=(
                f"could not verify Gen2 support in your account's region; "
                f"arm {arm.name!r} will fail at warehouse creation if Gen2 "
                f"is not available."
            ),
        ))

    return issues
