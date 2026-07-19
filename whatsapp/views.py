from datetime import timedelta

from django.contrib import messages
from django.db.models import Count, F, Max, Min
from django.db.models.functions import ExtractHour, ExtractIsoWeekDay, TruncDate
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views import View

from core.views import SuperuserRequiredMixin

from .models import Group, Member, Message

DAY_LABELS = ["Sen", "Sel", "Rab", "Kam", "Jum", "Sab", "Min"]  # ISO weekday 1..7


def _delta(curr, prev):
    """Trend-chip data comparing a current-period value to the prior period.

    Mirrors ``core.views._delta`` so the WhatsApp KPI tiles render the same
    up/down pill markup as the main dashboard.
    """
    pct = None if prev == 0 else round((curr - prev) / prev * 100)
    direction = "up" if curr > prev else "down" if curr < prev else "flat"
    return {"pct": pct, "dir": direction, "abs": curr - prev}


class WhatsAppActivityView(SuperuserRequiredMixin, View):
    """Staff dashboard: WhatsApp group activity insights — KPI tiles, activity
    charts, a peak-time heatmap, active/inactive breakdown and a re-engagement
    list, all scoped by group + a date window (defaults to the last 30 days).
    POST renames a group inline."""

    def get(self, request):
        group = request.GET.get("group") or ""
        start = request.GET.get("start") or ""
        end = request.GET.get("end") or ""

        # --- Window: default to the last 30 days when unset -------------------
        today = timezone.localdate()
        start_date = parse_date(start) or (today - timedelta(days=29))
        end_date = parse_date(end) or today
        if start_date > end_date:
            start_date, end_date = end_date, start_date
        start, end = start_date.isoformat(), end_date.isoformat()
        window_len = (end_date - start_date).days + 1
        prev_start = start_date - timedelta(days=window_len)
        prev_end = start_date - timedelta(days=1)

        # --- Base querysets (respect group + window) --------------------------
        msgs = Message.objects.all()
        if group:
            msgs = msgs.filter(group__group_id=group)
        base_qs = msgs.filter(created_on__date__gte=start_date, created_on__date__lte=end_date)
        prev_qs = msgs.filter(created_on__date__gte=prev_start, created_on__date__lte=prev_end)
        total = base_qs.count()

        # --- Sender leaderboard + per-group breakdown -------------------------
        leaderboard = (
            base_qs.values(phone=F("member__phone"), name=F("member__name"))
            .annotate(count=Count("id"), last_at=Max("created_on"))
            .order_by("-count")[:10]
        )
        groups = list(
            base_qs.values(gid=F("group__group_id"), gname=F("group__name"))
            .annotate(count=Count("id"), senders=Count("member", distinct=True))
            .order_by("-count")
        )

        # --- Roster / active / inactive --------------------------------------
        # Roster = members linked to >=1 group. bulk_members imports create
        # rostered-but-silent members, so the roster is usually larger than the
        # set of senders. "Inactive" = rostered, no message in the window.
        roster = (
            Member.objects.filter(groups__group_id=group)
            if group
            else Member.objects.filter(groups__isnull=False)
        ).distinct()
        active_ids = set(base_qs.values_list("member_id", flat=True).distinct())
        roster_count = roster.count()
        active_count = len(active_ids)
        inactive_count = max(roster_count - active_count, 0)
        participation = round(active_count / roster_count * 100) if roster_count else 0

        prev_total = prev_qs.count()
        prev_active = prev_qs.values("member_id").distinct().count()

        kpis = [
            {"label": "Total pesan", "value": total, "delta": _delta(total, prev_total)},
            {
                "label": "Anggota aktif",
                "value": active_count,
                "delta": _delta(active_count, prev_active),
            },
            {"label": "Tidak aktif", "value": inactive_count, "delta": None},
            {"label": "Partisipasi", "value": participation, "suffix": "%", "delta": None},
        ]

        # --- Activity over time (zero-filled daily series) -------------------
        per_day = {
            r["d"]: r
            for r in base_qs.annotate(d=TruncDate("created_on"))
            .values("d")
            .annotate(msgs=Count("id"), senders=Count("member", distinct=True))
        }
        activity = []
        cursor = start_date
        while cursor <= end_date:
            row = per_day.get(cursor)
            activity.append(
                {
                    "date": cursor.isoformat(),
                    "msgs": row["msgs"] if row else 0,
                    "senders": row["senders"] if row else 0,
                }
            )
            cursor += timedelta(days=1)

        # --- Activity segmentation (per-member message counts) ---------------
        low = regular = power = 0
        for r in base_qs.values("member_id").annotate(c=Count("id")):
            c = r["c"]
            if c >= 20:
                power += 1
            elif c >= 5:
                regular += 1
            else:  # 1..4 (members with 0 messages are not in base_qs)
                low += 1
        segments = [
            {"label": "Diam (0)", "value": inactive_count},
            {"label": "Rendah (1–4)", "value": low},
            {"label": "Rutin (5–19)", "value": regular},
            {"label": "Aktif (20+)", "value": power},
        ]
        donut = {"active": active_count, "inactive": inactive_count}

        # --- Peak-time heatmap (day-of-week x hour, local time) --------------
        tz = timezone.get_current_timezone()
        grid = [[0] * 24 for _ in range(7)]  # rows Mon..Sun (ISO weekday 1..7)
        heat_max = 0
        for r in (
            base_qs.annotate(
                h=ExtractHour("created_on", tzinfo=tz),
                d=ExtractIsoWeekDay("created_on", tzinfo=tz),
            )
            .values("d", "h")
            .annotate(c=Count("id"))
        ):
            grid[r["d"] - 1][r["h"]] = r["c"]
            heat_max = max(heat_max, r["c"])
        heatmap = []
        for i in range(7):
            cells = []
            for h in range(24):
                c = grid[i][h]
                # Floor nonzero cells so a single message stays visible.
                op = round(0.12 + 0.88 * (c / heat_max), 3) if c and heat_max else 0
                cells.append({"h": h, "c": c, "op": op})
            heatmap.append({"day": DAY_LABELS[i], "cells": cells})

        # --- New vs returning (first-ever message vs window start) -----------
        first_seen = dict(
            msgs.filter(member_id__in=active_ids)
            .values_list("member_id")
            .annotate(f=Min("created_on"))
        )
        new_count = sum(1 for f in first_seen.values() if f.date() >= start_date)
        returning_count = active_count - new_count

        # --- Per-group engagement rate ---------------------------------------
        roster_by_group = {
            g["group_id"]: g["n"]
            for g in Group.objects.values("group_id").annotate(n=Count("members", distinct=True))
        }
        for g in groups:
            r = roster_by_group.get(g["gid"], 0)
            g["roster"] = r
            g["rate"] = round(g["senders"] / r * 100) if r else 0

        # --- Inactive roster members (re-engagement list) --------------------
        inactive_members = (
            roster.exclude(id__in=active_ids)
            .annotate(last_seen=Max("messages__created_on"))
            .order_by(F("last_seen").desc(nulls_last=True), "name")
            .prefetch_related("groups")[:5]
        )

        # --- Quick-range presets ---------------------------------------------
        presets = []
        for label, days in (("7 hari", 7), ("30 hari", 30), ("90 hari", 90)):
            s = today - timedelta(days=days - 1)
            presets.append(
                {
                    "label": label,
                    "start": s.isoformat(),
                    "end": today.isoformat(),
                    "active": start == s.isoformat() and end == today.isoformat(),
                }
            )

        return render(
            request,
            "whatsapp/list.html",
            {
                "kpis": kpis,
                "activity": activity,
                "segments": segments,
                "donut": donut,
                "heatmap": heatmap,
                "new_count": new_count,
                "returning_count": returning_count,
                "inactive_members": inactive_members,
                "inactive_count": inactive_count,
                "active_count": active_count,
                "roster_count": roster_count,
                "participation": participation,
                "presets": presets,
                "leaderboard": leaderboard,
                "groups": groups,
                "group_options": Group.objects.order_by("name", "group_id"),
                "total": total,
                "group": group,
                "start": start,
                "end": end,
            },
        )

    def post(self, request):
        """Inline group rename from the dashboard."""
        group_id = request.POST.get("group_id")
        name = (request.POST.get("name") or "").strip() or None
        Group.objects.filter(group_id=group_id).update(name=name)
        messages.success(request, "Nama grup diperbarui.")
        return redirect(request.META.get("HTTP_REFERER") or "whatsapp")
