"""A fill count stored as Decimal must still be written on the decision row."""

from __future__ import annotations

import json
from decimal import Decimal
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.database.models import Base, DecisionLog
from src.json_safe import dumps_payload


def test_post_fill_action_record_accepts_decimal_fill_fields(tmp_path):
    action_taken = {
        "status": "zero_fill",
        "summary": "Exchange fill was zero.",
        "signal": "BUY_YES",
        "filled_fp": Decimal("0.00"),
        "fees": Decimal("0.07"),
        "at": datetime(2026, 11, 11, 18, 41, 15, tzinfo=timezone.utc),
    }
    text = dumps_payload(action_taken)
    loaded = json.loads(text)
    assert loaded["filled_fp"] == 0.0
    assert isinstance(loaded["filled_fp"], float)
    assert loaded["fees"] == 0.07
    assert loaded["at"].startswith("2026-11-11T18:41:15")

    engine = create_engine(f"sqlite:///{tmp_path / 'decisions.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    db.add(
        DecisionLog(
            id="dec-decimal",
            market_id="KXTEST",
            market_title="Decimal fill",
            decision="BUY_YES",
            confidence=0.7,
            action_taken=text,
            xai_analysis=dumps_payload({"fee": Decimal("0.02")}),
            key_factors=dumps_payload(["fill"]),
            market_context=dumps_payload({"yes_price": Decimal("0.65")}),
        )
    )
    db.commit()
    db.close()

    db = factory()
    row = db.query(DecisionLog).filter(DecisionLog.id == "dec-decimal").one()
    again = json.loads(row.action_taken)
    db.close()
    assert again["filled_fp"] == 0.0
    assert again["status"] == "zero_fill"
