"""Expose compatibility bindings for stored download-plan review endpoints."""

from app.services.assistant_reviews import plan_review, recent_plan_reviews

__all__ = ["plan_review", "recent_plan_reviews"]
