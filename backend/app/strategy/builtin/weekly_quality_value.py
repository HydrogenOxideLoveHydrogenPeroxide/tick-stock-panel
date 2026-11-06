"""Audited research extension of the weekly strategy; baseline remains available."""
from app.services.quality_value_strategy import make_adapter
globals().update(make_adapter('weekly'))
