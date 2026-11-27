"""Admin API for price validity: what the checks flagged and what the LLM said."""

from typing import Any

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select

from pos_coordinator.api.deps import CurrentViewer, Session
from pos_coordinator.models import Offer, PriceReview, Product
from pos_coordinator.validity import review

router = APIRouter(prefix="/api/admin")

FLAGS = (*review.REVIEW_FLAGS, review.FLAG)
VERDICTS = ("valid", "placeholder", "wrong_price_type", "pack_mismatch", "wrong_product", "unclear")


@router.get("/price-checks")
async def price_checks(
    admin: CurrentViewer, session: Session, verdict: str | None = None, limit: int = 100
) -> dict[str, Any]:
    """Offers per check flag, the reviews per verdict, and the latest reviews (with
    the offer and product) to read through."""
    if verdict is not None and verdict not in VERDICTS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "unknown verdict")
    flagged = {}
    for flag in FLAGS:
        flagged[flag] = (
            await session.execute(select(func.count()).where(Offer.flags.contains([flag])))
        ).scalar_one()
    q = (
        select(PriceReview, Offer, Product.title)
        .join(Offer, Offer.id == PriceReview.offer_id)
        .outerjoin(Product, Product.id == Offer.product_id)
        .order_by(PriceReview.created_at.desc())
        .limit(min(max(limit, 1), 500))
    )
    if verdict is not None:
        q = q.where(PriceReview.verdict == verdict)
    latest = (await session.execute(q)).all()
    return {
        "flagged": flagged,
        "reviews": await review.counts(session),
        "latest": [
            {
                "offer_id": o.id,
                "product_id": o.product_id,
                "title": title,
                "source": o.source,
                "url": o.url,
                "price": o.price,
                "currency": o.currency,
                "raw_price": o.raw_price,
                "flags": o.flags,
                "verdict": r.verdict,
                "confidence": r.confidence,
                "reason": r.reason,
                "version": r.validator_version,
                "at": r.created_at,
            }
            for r, o, title in latest
        ],
    }
