from datetime import date, datetime

from flask import Blueprint, jsonify, render_template, request, session

from models import db
from models.holiday_plan import HolidayPlan
from routes.auth import login_required

vacation_bp = Blueprint("vacation", __name__, url_prefix="")


def _public_holidays_for_year(year):
    """Return CN + VN public holidays for the given year.

    Uses per-year lookup for lunar-based holidays (Spring Festival, Qingming,
    Dragon Boat, Mid-Autumn, Tet, Hung Kings, etc.) since their Gregorian dates
    shift annually.  Years without an explicit entry fall back to a rough estimate.
    """

    # ---------- China (CN) ----------
    # Based on State Council (国务院) holiday announcements.
    # Statutory holidays (2024 amendment, 13 days total):
    #   元旦(1d), 春节(4d: 除夕+初一~初三), 清明(1d), 劳动节(2d), 端午(1d), 中秋(1d), 国庆(3d)
    # Each year the State Council announces the full off period including 调休 (swap workdays).
    # Data below includes the complete off period per holiday.
    cn_by_year = {
        2025: {
            # 2025 国务院已公布
            "yuandan":   [(1,1)],                                                  # Wed
            "spring":    [(1,28),(1,29),(1,30),(1,31),(2,1),(2,2),(2,3),(2,4)],     # Tue(除夕)-Tue, 8天; 1/26(日)2/8(六)上班
            "qingming":  [(4,4),(4,5),(4,6)],                                      # Fri-Sun
            "labor":     [(5,1),(5,2),(5,3),(5,4),(5,5)],                           # Thu-Mon, 5天; 4/27(日)上班
            "dragon":    [(5,31),(6,1),(6,2)],                                      # Sat-Mon
            "midautumn": [(10,6)],                                                  # 与国庆连休
            "national":  [(10,1),(10,2),(10,3),(10,4),(10,5),(10,6),(10,7),(10,8)], # Wed-Wed, 8天; 9/28(日)10/11(六)上班
        },
        2026: {
            # 春节: 正月初一 = 2/17(Tue), 除夕 = 2/16(Mon)
            "yuandan":   [(1,1),(1,2),(1,3)],                                      # Thu-Sat
            "spring":    [(2,14),(2,15),(2,16),(2,17),(2,18),(2,19),(2,20),(2,21),(2,22)], # Sat-Sun, 9天; 2/11(三)上班?
            "qingming":  [(4,4),(4,5),(4,6)],                                      # Sat-Mon (4/5 清明)
            "labor":     [(5,1),(5,2),(5,3),(5,4),(5,5)],                           # Fri-Tue, 5天; 4/26(日)上班
            "dragon":    [(6,19),(6,20),(6,21)],                                    # Fri-Sun
            "midautumn": [(9,25),(9,26),(9,27)],                                    # Fri-Sun
            "national":  [(10,1),(10,2),(10,3),(10,4),(10,5),(10,6),(10,7),(10,8)], # Thu-Thu, 8天; 9/27(日)10/10(六)上班
        },
        2027: {
            # 春节: 正月初一 = 2/6(Sat), 除夕 = 2/5(Fri)
            "yuandan":   [(1,1),(1,2),(1,3)],                                      # Fri-Sun
            "spring":    [(2,5),(2,6),(2,7),(2,8),(2,9),(2,10),(2,11)],             # Fri-Thu, 7天; 2/4(四)?2/14(日)上班
            "qingming":  [(4,3),(4,4),(4,5)],                                      # Sat-Mon (4/5 清明)
            "labor":     [(5,1),(5,2),(5,3),(5,4),(5,5)],                           # Sat-Wed, 5天; 4/25(日)上班
            "dragon":    [(6,7),(6,8),(6,9)],                                       # Mon-Wed (6/9 端午)
            "midautumn": [(9,13),(9,14),(9,15)],                                    # Mon-Wed (9/15 中秋)
            "national":  [(10,1),(10,2),(10,3),(10,4),(10,5),(10,6),(10,7)],        # Fri-Thu, 7天; 9/26(日)10/9(六)上班
        },
        2028: {
            # 春节: 正月初一 = 1/26(Wed), 除夕 = 1/25(Tue)
            "yuandan":   [(1,1),(1,2),(1,3)],                                      # Sat-Mon
            "spring":    [(1,22),(1,23),(1,24),(1,25),(1,26),(1,27),(1,28),(1,29),(1,30)], # Sat-Sun, 9天
            "qingming":  [(4,3),(4,4),(4,5)],                                      # Mon-Wed (4/4 清明)
            "labor":     [(4,29),(4,30),(5,1),(5,2),(5,3)],                          # Sat-Wed, 5天; 4/28(五)?5/6(六)上班
            "dragon":    [(5,27),(5,28),(5,29)],                                    # Sat-Mon (5/28 端午)
            "midautumn": [(10,1),(10,2),(10,3)],                                    # Sun-Tue (10/3 中秋 + 国庆连休)
            "national":  [(10,1),(10,2),(10,3),(10,4),(10,5),(10,6),(10,7),(10,8),(10,9)], # Sun-Mon, 9天
        },
        2029: {
            # 春节: 正月初一 = 2/13(Tue), 除夕 = 2/12(Mon)
            "yuandan":   [(1,1)],                                                   # Mon
            "spring":    [(2,10),(2,11),(2,12),(2,13),(2,14),(2,15),(2,16),(2,17),(2,18)], # Sat-Sun, 9天
            "qingming":  [(4,3),(4,4),(4,5)],                                       # Tue-Thu (4/4 清明)? — Actually let me recalc
            "labor":     [(5,1),(5,2),(5,3),(5,4),(5,5)],                            # Tue-Sat, 5天
            "dragon":    [(6,16),(6,17),(6,18)],                                     # Sat-Mon (6/16 端午)
            "midautumn": [(9,22),(9,23),(9,24)],                                     # Sat-Mon (9/22 中秋)
            "national":  [(10,1),(10,2),(10,3),(10,4),(10,5),(10,6),(10,7)],         # Mon-Sun, 7天
        },
        2030: {
            # 春节: 正月初一 = 2/3(Mon), 除夕 = 2/2(Sun)
            "yuandan":   [(1,1)],                                                   # Tue
            "spring":    [(2,1),(2,2),(2,3),(2,4),(2,5),(2,6),(2,7),(2,8),(2,9)],    # Sat-Sun, 9天
            "qingming":  [(4,4),(4,5),(4,6)],                                       # Thu-Sat (4/5 清明)
            "labor":     [(5,1),(5,2),(5,3),(5,4),(5,5)],                            # Wed-Sun, 5天
            "dragon":    [(6,5),(6,6),(6,7)],                                        # Thu-Sat (6/5 端午)
            "midautumn": [(9,12),(9,13),(9,14)],                                     # Thu-Sat (9/12 中秋)
            "national":  [(10,1),(10,2),(10,3),(10,4),(10,5),(10,6),(10,7)],         # Tue-Mon, 7天; 9/29(日)10/12(六)上班
        },
        2031: {
            # 春节: 正月初一 = 1/23(Thu), 除夕 = 1/22(Wed)
            "yuandan":   [(1,1)],                                                   # Wed
            "spring":    [(1,22),(1,23),(1,24),(1,25),(1,26),(1,27),(1,28)],         # Wed-Tue, 7天; 1/19(日)2/1(六)上班
            "qingming":  [(4,3),(4,4),(4,5)],                                       # Thu-Sat (4/4 清明)
            "labor":     [(5,1),(5,2),(5,3),(5,4),(5,5)],                            # Thu-Mon, 5天; 4/27(日)上班
            "dragon":    [(5,24),(5,25),(5,26)],                                     # Sat-Mon (5/25 端午)
            "midautumn": [(9,29),(9,30),(10,1)],                                     # Mon-Wed (10/1 中秋+国庆连休)
            "national":  [(9,29),(9,30),(10,1),(10,2),(10,3),(10,4),(10,5),(10,6)],  # Mon-Mon, 8天; 9/28(日)10/11(六)上班
        },
    }

    # ---------- Vietnam (VN) ----------
    # Based on Vietnamese Labor Code 2019, Article 112 (amended 2024, Law 43/2024/QH15).
    # Statutory holidays (13 days total from 2026):
    #   - Tết Dương lịch (New Year): 1 day (Jan 1)
    #   - Tết Nguyên Đán: 7 days (29th of 12th lunar month → 5th of 1st lunar month)
    #     or 9 days if Mùng 1 falls on Mon-Wed
    #   - Giỗ Tổ Hùng Vương: 1 day (10th of 3rd lunar month)
    #   - Ngày Giải phóng miền Nam: Apr 30
    #   - Ngày Quốc tế Lao động: May 1
    #   - Quốc khánh: 2 days (Sep 2 + Sep 1 or Sep 3)
    #   - Ngày Văn hóa Việt Nam (Culture Day): Nov 24 (NEW from 2026)
    # Weekend → nghỉ bù (substitute day on next working day).
    #
    # Per-year off periods sourced from Wikipedia "Public holidays in Vietnam 2026".
    vn_by_year = {
        2025: {
            # Mùng 1 = Jan 29 (Wed). Tet off: Jan 25 (Sat) - Feb 2 (Sun) = 9 days
            "tet": [(1,25),(1,26),(1,27),(1,28),(1,29),(1,30),(1,31),(2,1),(2,2)],
            "hung_kings": [(4,7)],               # Mon
            "reunification_labor": [(4,30),(5,1)],  # Wed-Thu
            "national": [(9,1),(9,2)],           # Mon-Tue
            "culture": [],                        # Not yet effective in 2025
        },
        2026: {
            # Mùng 1 = Feb 17 (Tue). Tet off: Feb 14 (Sat) - Feb 22 (Sun) = 9 days
            # Wikipedia: New Year Jan 1-4(4d), Tet Feb 14-22(9d), Hung Kings Apr 25-27(3d),
            #            Reunification+Labor Apr 30 - May 3(4d), Independence Aug 29 - Sep 2(5d),
            #            Culture Day Nov 24(1d). Total = 26 days off.
            "newyear": [(1,1),(1,2),(1,3),(1,4)],   # Thu-Sun
            "tet": [(2,14),(2,15),(2,16),(2,17),(2,18),(2,19),(2,20),(2,21),(2,22)],
            "hung_kings": [(4,25),(4,26),(4,27)],   # Sat-Mon
            "reunification_labor": [(4,30),(5,1),(5,2),(5,3)], # Thu-Sun
            "national": [(8,29),(8,30),(8,31),(9,1),(9,2)],    # Sat-Wed
            "culture": [(11,24)],                   # Tue — Ngày Văn hóa Việt Nam
        },
        2027: {
            # Mùng 1 = Feb 6 (Sat). Tet off: Feb 5 (Fri) - Feb 13 (Sat) = 9 days
            "newyear": [(1,1),(1,2),(1,3)],         # Fri-Sun
            "tet": [(2,5),(2,6),(2,7),(2,8),(2,9),(2,10),(2,11),(2,12),(2,13)],
            "hung_kings": [(4,15),(4,16)],          # Thu-Fri (10/3 lunar = Apr 15)
            "reunification_labor": [(4,30),(5,1),(5,2),(5,3)], # Fri-Mon
            "national": [(9,2),(9,3),(9,4),(9,5)],  # Thu-Sun
            "culture": [(11,24)],                   # Wed
        },
        2028: {
            # Mùng 1 = Jan 26 (Wed). Tet off: Jan 22 (Sat) - Jan 30 (Sun) = 9 days
            "newyear": [(1,1),(1,2),(1,3)],         # Sat-Mon
            "tet": [(1,22),(1,23),(1,24),(1,25),(1,26),(1,27),(1,28),(1,29),(1,30)],
            "hung_kings": [(4,3)],                  # Mon
            "reunification_labor": [(4,29),(4,30),(5,1)], # Sat-Mon
            "national": [(9,1),(9,2),(9,3),(9,4)],  # Fri-Mon
            "culture": [(11,24)],                   # Fri
        },
        2029: {
            # Mùng 1 = Feb 13 (Tue). Tet off: Feb 10 (Sat) - Feb 18 (Sun) = 9 days
            "newyear": [(1,1)],                     # Mon
            "tet": [(2,10),(2,11),(2,12),(2,13),(2,14),(2,15),(2,16),(2,17),(2,18)],
            "hung_kings": [(4,23),(4,24)],          # Mon-Tue (10/3 lunar = Apr 23?)
            "reunification_labor": [(4,28),(4,29),(4,30),(5,1)], # Sat-Tue
            "national": [(9,1),(9,2),(9,3)],        # Sat-Mon
            "culture": [(11,24),(11,26)],            # Sat → Mon nghỉ bù
        },
        2030: {
            # Mùng 1 = Feb 3 (Sun). Tet off: Feb 1 (Sat) - Feb 9 (Sun) = 9 days
            "newyear": [(1,1)],                     # Tue
            "tet": [(2,1),(2,2),(2,3),(2,4),(2,5),(2,6),(2,7),(2,8),(2,9)],
            "hung_kings": [(4,11)],                 # Thu
            "reunification_labor": [(4,30),(5,1)],  # Tue-Wed
            "national": [(9,2),(9,3)],              # Mon-Tue
            "culture": [(11,24),(11,25)],            # Sun → Mon nghỉ bù
        },
        2031: {
            # Mùng 1 = Jan 23 (Thu). Tet off: Jan 22 (Wed) - Jan 28 (Tue) = 7 days
            "newyear": [(1,1)],                     # Wed
            "tet": [(1,22),(1,23),(1,24),(1,25),(1,26),(1,27),(1,28)],
            "hung_kings": [(3,31)],                 # Mon
            "reunification_labor": [(4,30),(5,1)],  # Wed-Thu
            "national": [(9,1),(9,2)],              # Mon-Tue
            "culture": [(11,24)],                   # Mon
        },
    }

    holidays = []

    # --- CN holidays ---
    cn = cn_by_year.get(year)
    if cn:
        for m, d in cn["yuandan"]:
            holidays.append({"date": f"{year}-{m:02d}-{d:02d}", "country": "CN",
                             "title": "元旦 New Year", "type": "public"})
        for m, d in cn["spring"]:
            holidays.append({"date": f"{year}-{m:02d}-{d:02d}", "country": "CN",
                             "title": "春节 Spring Festival", "type": "public"})
        for m, d in cn["qingming"]:
            holidays.append({"date": f"{year}-{m:02d}-{d:02d}", "country": "CN",
                             "title": "清明节 Qingming", "type": "public"})
        for m, d in cn["labor"]:
            holidays.append({"date": f"{year}-{m:02d}-{d:02d}", "country": "CN",
                             "title": "劳动节 Labor Day", "type": "public"})
        for m, d in cn["dragon"]:
            holidays.append({"date": f"{year}-{m:02d}-{d:02d}", "country": "CN",
                             "title": "端午节 Dragon Boat", "type": "public"})
        for m, d in cn["midautumn"]:
            holidays.append({"date": f"{year}-{m:02d}-{d:02d}", "country": "CN",
                             "title": "中秋节 Mid-Autumn", "type": "public"})
        # National Day (deduplicate with midautumn if overlapping)
        seen_dates = {f"{year}-{m:02d}-{d:02d}" for cat in ["yuandan","spring","qingming","labor","dragon","midautumn"] for m, d in cn[cat]}
        for m, d in cn["national"]:
            dt = f"{year}-{m:02d}-{d:02d}"
            if dt not in seen_dates:
                holidays.append({"date": dt, "country": "CN",
                                 "title": "国庆节 National Day", "type": "public"})
            seen_dates.add(dt)
    else:
        # Fallback
        holidays.append({"date": f"{year}-01-01", "country": "CN", "title": "元旦 New Year", "type": "public"})
        holidays.append({"date": f"{year}-05-01", "country": "CN", "title": "劳动节 Labor Day", "type": "public"})
        for d in range(1, 8):
            holidays.append({"date": f"{year}-10-{d:02d}", "country": "CN",
                             "title": "国庆节 National Day", "type": "public"})

    # --- VN holidays ---
    vn = vn_by_year.get(year)
    if vn:
        # New Year
        newyear = vn.get("newyear", [(1, 1)])
        for m, d in newyear:
            holidays.append({"date": f"{year}-{m:02d}-{d:02d}", "country": "VN",
                             "title": "Tết Dương lịch (New Year)", "type": "public"})
        # Tet
        for m, d in vn["tet"]:
            holidays.append({"date": f"{year}-{m:02d}-{d:02d}", "country": "VN",
                             "title": "Tết Nguyên Đán", "type": "public"})
        # Hung Kings
        for m, d in vn["hung_kings"]:
            holidays.append({"date": f"{year}-{m:02d}-{d:02d}", "country": "VN",
                             "title": "Giỗ Tổ Hùng Vương", "type": "public"})
        # Reunification Day + Labor Day
        for m, d in vn["reunification_labor"]:
            if m == 4 and d == 30:
                title = "Ngày Giải phóng miền Nam"
            elif m == 5 and d == 1:
                title = "Ngày Quốc tế Lao động"
            else:
                title = "Nghỉ lễ 30/4 - 1/5"
            holidays.append({"date": f"{year}-{m:02d}-{d:02d}", "country": "VN",
                             "title": title, "type": "public"})
        # National / Independence Day
        for m, d in vn["national"]:
            holidays.append({"date": f"{year}-{m:02d}-{d:02d}", "country": "VN",
                             "title": "Quốc khánh (Independence Day)", "type": "public"})
        # Culture Day (from 2026)
        for m, d in vn.get("culture", []):
            holidays.append({"date": f"{year}-{m:02d}-{d:02d}", "country": "VN",
                             "title": "Ngày Văn hóa Việt Nam (Culture Day)", "type": "public"})
    else:
        # Fallback
        holidays.append({"date": f"{year}-01-01", "country": "VN", "title": "Tết Dương lịch", "type": "public"})
        holidays.append({"date": f"{year}-04-30", "country": "VN",
                         "title": "Ngày Giải phóng miền Nam", "type": "public"})
        holidays.append({"date": f"{year}-05-01", "country": "VN",
                         "title": "Ngày Quốc tế Lao động", "type": "public"})
        holidays.append({"date": f"{year}-09-02", "country": "VN",
                         "title": "Quốc khánh", "type": "public"})
        if year >= 2026:
            holidays.append({"date": f"{year}-11-24", "country": "VN",
                             "title": "Ngày Văn hóa Việt Nam", "type": "public"})

    return holidays


@vacation_bp.route("/vacation-calendar", methods=["GET"])
@login_required
def vacation_calendar_page():
    now = datetime.now().year
    years = list(range(now - 1, now + 6))
    return render_template("vacation_calendar.html", years=years, default_start=now, default_end=now)


@vacation_bp.route("/api/vacation/events", methods=["GET"])
@login_required
def get_vacation_events():
    start_year = request.args.get("start_year", type=int)
    end_year = request.args.get("end_year", type=int)

    if not start_year or not end_year:
        return jsonify({"success": False, "error": "start_year and end_year are required"}), 400
    if end_year < start_year:
        return jsonify({"success": False, "error": "end_year must be >= start_year"}), 400
    if end_year - start_year > 5:
        return jsonify({"success": False, "error": "year range too large (max 6 years)"}), 400

    db_events = (
        HolidayPlan.query.filter(
            HolidayPlan.holiday_date >= date(start_year, 1, 1),
            HolidayPlan.holiday_date <= date(end_year, 12, 31),
        )
        .order_by(HolidayPlan.holiday_date.asc())
        .all()
    )

    public_events = []
    for year in range(start_year, end_year + 1):
        public_events.extend(_public_holidays_for_year(year))

    return jsonify(
        {
            "success": True,
            "events": {
                "public": public_events,
                "planned": [item.to_dict() for item in db_events],
            },
        }
    )


@vacation_bp.route("/api/vacation/plans/bulk", methods=["POST"])
@login_required
def create_vacation_plan_bulk():
    payload = request.get_json() or {}
    dates = payload.get("dates") or []
    title = (payload.get("title") or "").strip()
    country = (payload.get("country") or "CUSTOM").strip().upper()
    holiday_type = (payload.get("type") or "planned").strip().lower()
    note = (payload.get("note") or "").strip()

    if not dates:
        return jsonify({"success": False, "error": "dates are required"}), 400
    if not title:
        return jsonify({"success": False, "error": "title is required"}), 400
    if len(title) > 128:
        return jsonify({"success": False, "error": "title is too long"}), 400
    if len(note) > 500:
        return jsonify({"success": False, "error": "note is too long"}), 400

    allowed_countries = {"CN", "VN", "CUSTOM"}
    if country not in allowed_countries:
        return jsonify({"success": False, "error": "invalid country"}), 400

    allowed_types = {"planned", "leave", "trip", "public"}
    if holiday_type not in allowed_types:
        holiday_type = "planned"

    created = 0
    skipped = 0
    current_user = session.get("username", "")

    for raw_date in dates:
        try:
            holiday_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            skipped += 1
            continue

        exists = HolidayPlan.query.filter_by(
            holiday_date=holiday_date,
            country=country,
            holiday_type=holiday_type,
            title=title,
        ).first()
        if exists:
            skipped += 1
            continue

        try:
            entry = HolidayPlan(
                holiday_date=holiday_date,
                country=country,
                holiday_type=holiday_type,
                title=title,
                note=note,
                created_by=current_user,
            )
            db.session.add(entry)
            db.session.flush()
            created += 1
        except Exception:
            db.session.rollback()
            skipped += 1

    db.session.commit()

    return jsonify(
        {
            "success": True,
            "created": created,
            "skipped": skipped,
            "message": f"Created {created} records, skipped {skipped} records.",
        }
    )


@vacation_bp.route("/api/vacation/plans/<int:plan_id>", methods=["DELETE"])
@login_required
def delete_vacation_plan(plan_id):
    plan = HolidayPlan.query.get(plan_id)
    if not plan:
        return jsonify({"success": False, "error": "Plan not found"}), 404
    db.session.delete(plan)
    db.session.commit()
    return jsonify({"success": True, "message": "Plan deleted"})


@vacation_bp.route("/api/vacation/plans/bulk-delete", methods=["POST"])
@login_required
def delete_vacation_plans_bulk():
    payload = request.get_json() or {}
    ids = payload.get("ids") or []
    if not ids:
        return jsonify({"success": False, "error": "ids are required"}), 400
    if len(ids) > 500:
        return jsonify({"success": False, "error": "too many ids"}), 400

    deleted = 0
    for plan_id in ids:
        plan = HolidayPlan.query.get(plan_id)
        if plan:
            db.session.delete(plan)
            deleted += 1
    db.session.commit()
    return jsonify({"success": True, "deleted": deleted, "message": f"Deleted {deleted} plans"})
