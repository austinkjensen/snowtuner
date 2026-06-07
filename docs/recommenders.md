# Adding a recommender

A recommender is a self-contained module that:

1. Declares when it has enough data to be useful (`TrainingGate`).
2. Computes whatever stats it needs over `raw.*` and `features.*` tables (`fit`).
3. Emits `Recommendation` objects wrapping typed `Action` instances (`predict`).

Total scope of a typical built-in: ~150–250 lines, single file.

## The contract

Every recommender subclasses `Recommender` from
[`snowtuner/recommenders/base.py`](../src/snowtuner/recommenders/base.py):

```python
from snowtuner.actions.base import ActionType
from snowtuner.recommenders.base import Recommender, TrainingGate, ReadinessReport

class MyRecommender(Recommender):
    name = "my_recommender"           # globally unique across the registry
    version = "0.1.0"
    action_type = ActionType.ALTER_WAREHOUSE  # the action type it produces
    required_feature_tables: set[str] = set()
    training_gate: TrainingGate = ...

    def fit(self, conn) -> dict:
        """Compute and return JSON-serializable model state.
        Persisted to app.training_state."""

    def predict(self, conn, model_state) -> list[Recommendation]:
        """Emit recommendations from the persisted state."""
```

## Worked example: cost-minimizing AUTO_SUSPEND

The reference implementation lives at
[`builtins/auto_suspend_survival.py`](../src/snowtuner/recommenders/builtins/auto_suspend_survival.py).
Key shape:

```python
class AutoSuspendReadinessGate(TrainingGate):
    def evaluate(self, conn) -> ReadinessReport:
        # Need ≥ N suspend/resume cycles per warehouse.
        rows = conn.execute("""
            SELECT warehouse_name, COUNT(*) AS c
            FROM raw.warehouse_events_history
            WHERE event_name IN ('SUSPEND_WAREHOUSE', 'RESUME_WAREHOUSE')
            GROUP BY warehouse_name
        """).fetchall()
        ready = [w for w, c in rows if c >= MIN_CYCLES * 2]
        return ReadinessReport(
            is_ready=bool(ready),
            reason=f"{len(ready)} warehouses ready" if ready else "not enough cycles",
            signals={"ready_warehouses": ready},
        )

class AutoSuspendSurvivalTuner(Recommender):
    name = "auto_suspend_survival_tuner"
    action_type = ActionType.ALTER_WAREHOUSE
    training_gate = AutoSuspendReadinessGate()

    def fit(self, conn) -> dict:
        # Pull reactivation-gap samples per warehouse, fit empirical curves.
        # Return the per-warehouse stats the predictor will need.
        return {"per_warehouse": ...}

    def predict(self, conn, state) -> list[Recommendation]:
        # For each warehouse, find the AS that minimizes E[min(T,AS) + C·1{T>AS}].
        # Build AlterWarehouse actions, wrap in Recommendation, return.
        ...
```

The orchestrator persists `model_state` via `TrainingStateStore` automatically;
`predict` reads what `fit` produced. Each run re-fits from scratch, so the
state dict represents "the most recent observation," not learned weights.

## Producing recommendations

Wrap a typed `Action` subclass in a `Recommendation`. For an
`ALTER WAREHOUSE` recommendation:

```python
from snowtuner.actions import AlterWarehouse, WarehouseKnob
from snowtuner.actions.alter_warehouse import KnobChange
from snowtuner.recommendations.model import (
    EvidenceRef, Impact, Recommendation,
)

action = AlterWarehouse(
    warehouse_name=wh,
    changes=[KnobChange(
        knob=WarehouseKnob.AUTO_SUSPEND,
        current_value=current_as,           # for rendering rollback SQL
        proposed_value=proposed_as,
    )],
)

rec = Recommendation(
    generated_by=self.generated_by,           # property from base; auto-formats name@version
    action=action,
    rationale="Plain-English explanation the user reads in the UI.",
    evidence=[
        EvidenceRef(kind="warehouse_events", description="...", metric="...", value=...),
    ],
    expected_impact=Impact(
        credits_delta_daily=-1.23,            # negative = savings
        confidence=0.85,
        notes="based on N observed cycles",
    ),
)
```

Don't render SQL strings yourself in the recommender - `Action.to_sql()` does
that. The orchestrator persists each recommendation through
`RecommendationStore.insert(rec)` and supersedes prior open proposals from
the same recommender.

## Registering it

Edit [`recommenders/registry.py`](../src/snowtuner/recommenders/registry.py)
and add to `default_registry`:

```python
def default_registry() -> RecommenderRegistry:
    from snowtuner.recommenders.builtins.my_recommender import MyRecommender
    reg = RecommenderRegistry()
    reg.register(AutoSuspendSurvivalTuner())
    reg.register(RuleBasedRightSizer())
    reg.register(MyRecommender())  # ← here
    return reg
```

There is no third-party plugin discovery in v0.1 - registrations are explicit.

## Things to avoid

- **Don't emit raw SQL** in the recommendation. Always go through `Action`.
- **Don't write to `raw.*` or `features.*`** during predict. Predicts are
  pure functions of (already-built feature tables, persisted model state).
- **Don't share state between fit/predict via class attributes.** Use the
  `model_state` dict the orchestrator round-trips.
- **Don't forget to set `current_value` on each `KnobChange`** - that's how
  rollback SQL gets rendered.
- **Don't compete with another recommender for the same `target_resource()`**
  unless you want them to mutually-supersede. `AlterWarehouse.target_resource`
  is scoped by knob set, so two recommenders both touching `AUTO_SUSPEND` on
  the same warehouse will collide; one touching `AUTO_SUSPEND` and another
  touching `WAREHOUSE_SIZE` won't.

## Adding a new feature transform (if needed)

If your recommender needs a derived input that doesn't exist yet, write a
`FeatureTransform` in `features/library/` and register it in
`features/__init__.py:DEFAULT_TRANSFORMS`. Declare the tables it `inputs`
and `outputs` so the pipeline can topologically order it.

```python
class MyTransform(FeatureTransform):
    name = "my_feature"
    inputs = {"raw.query_history"}
    outputs = {"features.my_feature"}

    def run(self, conn) -> None:
        conn.execute("DELETE FROM features.my_feature")
        conn.execute("INSERT INTO features.my_feature SELECT ...")
```

Add the new table's DDL to `storage/schema.py:_DDL` so it gets created on
startup. The pipeline's topological sort will run your transform after any
upstream transforms it depends on.
