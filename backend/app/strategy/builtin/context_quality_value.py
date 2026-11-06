"""Audited research extension of the environment/sentiment daily submodel."""
from app.services.quality_value_strategy import make_adapter
globals().update(make_adapter('context'))
