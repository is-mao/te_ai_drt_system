from datetime import datetime

from models import db


class HolidayPlan(db.Model):
    __tablename__ = "holiday_plans"

    id = db.Column(db.Integer, primary_key=True)
    holiday_date = db.Column(db.Date, nullable=False, index=True)
    country = db.Column(db.String(16), nullable=False, default="CUSTOM")
    holiday_type = db.Column(db.String(32), nullable=False, default="planned")
    title = db.Column(db.String(128), nullable=False)
    note = db.Column(db.String(500), nullable=False, default="")
    created_by = db.Column(db.String(64), nullable=False, default="")
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint("holiday_date", "country", "holiday_type", "title", name="uq_holiday_plan"),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "date": self.holiday_date.isoformat(),
            "country": self.country,
            "type": self.holiday_type,
            "title": self.title,
            "note": self.note,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
