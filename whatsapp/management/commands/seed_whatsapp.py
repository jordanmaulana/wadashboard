"""Seed demo WhatsApp group activity so every /whatsapp/ dashboard widget lights up.

Populates Group/Member/Message rows spread across the dashboard's 30-day window
(and the prior period for KPI deltas): an activity chart, peak-time heatmap,
activity segmentation, active-vs-inactive donut, new-vs-returning split, per-group
participation, leaderboard and re-engagement list all get realistic data.

Always starts fresh: every run first flushes the previously-seeded demo rows —
identified by the group_id / phone sentinel prefixes — then re-creates them. Flush +
reseed run in a single transaction, so a failed run never leaves the demo world
half-wiped. Only sentinel-tagged rows are touched; real relay-ingested data is never
deleted.

    uv run manage.py seed_whatsapp        # wipe demo WA data, then reseed
"""

import random
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from whatsapp.models import Group, Member, Message

# WhatsApp demo sentinels — the group_id / phone prefixes tag demo rows so the
# flush only ever removes seeded WhatsApp data, never real relay-ingested rows.
WA_GROUP_PREFIX = "demo-wa-"
WA_PHONE_PREFIX = "6289900"

# Three groups; the third is left unnamed so the dashboard's inline-rename UI and
# the Group.__str__ group_id fallback are demonstrable. (group_id, name)
DEMO_WA_GROUPS = [
    (f"{WA_GROUP_PREFIX}a", "Kelas Pagi A1"),
    (f"{WA_GROUP_PREFIX}b", "Kelas Malam B1"),
    (f"{WA_GROUP_PREFIX}c", None),
]

# Members keyed by an activity `tier` that drives message volume so every
# dashboard widget lights up:
#   power         -> 20+ window msgs  (Aktif 20+ bucket, leaderboard top)
#   regular       -> 5–19 window msgs (Rutin bucket)
#   low           -> 1–4 window msgs  (Rendah bucket)
#   silent_history-> 0 window msgs, but a message 30–55 days ago (inactive list
#                    shows a real last_seen; feeds the prior-period KPI delta)
#   silent_never  -> never messaged   (inactive list "Belum pernah", Diam bucket)
# `returning` gives an otherwise window-only member an extra >30-day-old message
# so the new-vs-returning split is real. `groups` are indices into DEMO_WA_GROUPS
# and are intentionally uneven so per-group engagement rates differ.
DEMO_WA_MEMBERS = [
    {"name": "Rina", "tier": "power", "groups": [0], "returning": True},
    {"name": "Agus", "tier": "power", "groups": [0, 1], "returning": False},
    {"name": "Sinta", "tier": "regular", "groups": [0], "returning": True},
    {"name": "Bagus", "tier": "regular", "groups": [1], "returning": False},
    {"name": "Wati", "tier": "regular", "groups": [1, 2], "returning": False},
    {"name": "Dodi", "tier": "low", "groups": [0], "returning": False},
    {"name": "Fitri", "tier": "low", "groups": [1], "returning": True},
    {"name": "Hadi", "tier": "low", "groups": [2], "returning": False},
    {"name": "Indra", "tier": "low", "groups": [0], "returning": False},
    {"name": "Joko", "tier": "silent_history", "groups": [0], "returning": False},
    {"name": "Kiki", "tier": "silent_history", "groups": [2], "returning": False},
    {"name": "Lina", "tier": "silent_never", "groups": [1], "returning": False},
    {"name": "Maya", "tier": "silent_never", "groups": [2], "returning": False},
    {"name": "Nanda", "tier": "silent_never", "groups": [0], "returning": False},
]

# Window-message count range per active tier.
WA_TIER_VOLUME = {"power": (20, 27), "regular": (6, 15), "low": (1, 4)}

# Short chat snippets (mixed BI/EN, as a real class group reads).
DEMO_WA_TEXTS = [
    "Selamat pagi semua!",
    "Sudah kerjakan PR belum?",
    "Thank you, teacher!",
    "See you tomorrow 🙌",
    "Maaf telat gabung",
    "Boleh minta rekaman kelas?",
    "I think the answer is B",
    "Setuju banget",
    "Nice explanation!",
    "Ada yang punya catatan?",
    "Hadir 🙋",
    "How do you say 'terlambat' in English?",
]


class Command(BaseCommand):
    help = "Seed demo WhatsApp group activity for the /whatsapp/ dashboard."

    def handle(self, *args, **options):
        with transaction.atomic():
            self._flush()
            counts = self._seed()

        self.stdout.write(
            self.style.SUCCESS(
                "Seeded WhatsApp demo data — created: "
                f"{counts['wa_groups']} groups, {counts['wa_members']} members, "
                f"{counts['wa_messages']} messages (0 means already present)."
            )
        )

    def _flush(self):
        # Message.member / .group are PROTECT -> messages first.
        wmsg, _ = Message.objects.filter(group__group_id__startswith=WA_GROUP_PREFIX).delete()
        wmem, _ = Member.objects.filter(phone__startswith=WA_PHONE_PREFIX).delete()
        wgrp, _ = Group.objects.filter(group_id__startswith=WA_GROUP_PREFIX).delete()

        self.stdout.write(
            self.style.WARNING(
                f"Flushed demo data: {wmsg} messages, {wmem} members, {wgrp} groups."
            )
        )

    def _seed(self):
        """Seed Group/Member/Message rows so every /whatsapp/ dashboard widget has data.

        Message.created_on is auto_now_add (BaseModel), so it is ignored at
        create() time — each message is backdated with a queryset .update(), the
        same pattern the whatsapp tests use, to spread activity across the
        dashboard's 30-day window (and the prior period for KPI deltas).
        """
        counts = dict.fromkeys(["wa_groups", "wa_members", "wa_messages"], 0)
        rng = random.Random(20260717)  # fixed seed -> reproducible spread
        now = timezone.now()

        groups = []
        for gid, name in DEMO_WA_GROUPS:
            group, created = Group.objects.get_or_create(group_id=gid, defaults={"name": name})
            counts["wa_groups"] += int(created)
            groups.append(group)

        def _window_dt():
            # tz-aware datetime in the last 29 days, active hours 06:00–22:00.
            base = now - timedelta(days=rng.randint(0, 29))
            return base.replace(
                hour=rng.randint(6, 22), minute=rng.randint(0, 59), second=0, microsecond=0
            )

        def _prior_dt():
            # 30–55 days ago: the dashboard's prior-period (KPI delta / returning).
            base = now - timedelta(days=rng.randint(30, 55))
            return base.replace(
                hour=rng.randint(6, 22), minute=rng.randint(0, 59), second=0, microsecond=0
            )

        def _mk_message(member, group, when):
            msg = Message.objects.create(
                member=member, group=group, message=rng.choice(DEMO_WA_TEXTS)
            )
            Message.objects.filter(id=msg.id).update(created_on=when)
            counts["wa_messages"] += 1

        for i, spec in enumerate(DEMO_WA_MEMBERS):
            phone = f"{WA_PHONE_PREFIX}{i:04d}"
            member, created = Member.objects.get_or_create(
                phone=phone, defaults={"name": spec["name"]}
            )
            counts["wa_members"] += int(created)
            member_groups = [groups[gi] for gi in spec["groups"]]
            member.groups.add(*member_groups)

            tier = spec["tier"]
            if tier in WA_TIER_VOLUME:  # active: power / regular / low
                lo, hi = WA_TIER_VOLUME[tier]
                for _ in range(rng.randint(lo, hi)):
                    _mk_message(member, rng.choice(member_groups), _window_dt())
                if spec["returning"]:  # an older message -> counts as returning, not new
                    _mk_message(member, member_groups[0], _prior_dt())
            elif tier == "silent_history":  # inactive now, seen before -> real last_seen
                for _ in range(rng.randint(1, 2)):
                    _mk_message(member, member_groups[0], _prior_dt())
            # silent_never: rostered but no messages at all.

        return counts
