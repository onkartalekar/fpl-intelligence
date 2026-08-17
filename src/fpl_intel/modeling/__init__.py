"""The projection and decision-recommendation pipeline -- see `MODEL.md` for the full prose
treatment and `ARCHITECTURE.md`'s "Model pipeline" diagram for the pipeline shape this package
implements.

`coefficients.py` (fitted model coefficients), `minutes.py`/`ml_minutes.py` (expected-minutes
model + its shadow ML challenger), `projection.py` (component-level scoring), `team_strength.py`
(Dixon-Coles ratings), `recommendations.py` (squad construction), `transfer_decisions.py`
(roll/transfer/chip scenarios), `backtest.py` (no-lookahead historical backtest),
`model_performance.py` (immutable snapshots + post-gameweek evaluation).
"""
