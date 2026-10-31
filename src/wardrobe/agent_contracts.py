"""Business inputs for recommendation operations."""

from .contracts import StrictModel
from .recommendations import RecommendationInput


class AgentRecommendation(RecommendationInput):
    pass


class AgentOperationCancel(StrictModel):
    """Recommendation cancellation has no business input fields."""
