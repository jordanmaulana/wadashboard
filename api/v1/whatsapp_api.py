import logging

from django.conf import settings
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from api.v1.serializers import (
    WhatsAppBulkMembersSerializer,
    WhatsAppIngestSerializer,
    WhatsAppSeedGroupsSerializer,
)
from whatsapp.models import Group, Member, Message

logger = logging.getLogger(__name__)


def _verify(request) -> bool:
    """Shared-secret check for the WhatsApp relay, mirroring the Mayar webhook."""
    expected = settings.WHATSAPP_WEBHOOK_TOKEN
    if not expected:
        logger.error("whatsapp webhook: WHATSAPP_WEBHOOK_TOKEN not configured")
        return False
    received = request.headers.get("X-Callback-Token") or request.headers.get("x-callback-token")
    return bool(received) and received == expected


@api_view(["POST"])
@permission_classes([AllowAny])
def ingest(request):
    """Log one WhatsApp group message posted by an external relay."""
    if not _verify(request):
        return Response({"detail": "token tidak valid"}, status=401)
    serializer = WhatsAppIngestSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    group, _ = Group.objects.get_or_create(group_id=data["group_id"])
    member, created = Member.objects.get_or_create(
        phone=data["phone"], defaults={"name": data.get("name", "")}
    )
    # Keep the member's display name current with the latest message.
    if not created and data.get("name") and member.name != data["name"]:
        member.name = data["name"]
        member.save(update_fields=["name"])

    member.groups.add(group)  # idempotent — no duplicate through-rows

    msg = Message.objects.create(message=data.get("message", ""), member=member, group=group)
    return Response({"ok": True, "id": msg.id}, status=201)


@api_view(["POST"])
@permission_classes([AllowAny])
def bulk_members(request):
    """Attach a batch of phone numbers to one WhatsApp group (upsert Members).

    Non-atomic: blank / over-length / intra-batch-duplicate phones are skipped and
    echoed back in `skipped`; valid phones are upserted as Members and linked to
    the group. No Message is created.
    """
    if not _verify(request):
        return Response({"detail": "token tidak valid"}, status=401)
    serializer = WhatsAppBulkMembersSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    group, _ = Group.objects.get_or_create(group_id=data["group_id"])

    created = 0
    linked = 0
    skipped = []
    seen = set()
    for raw in data["phones"]:
        phone = (raw or "").strip()
        if not phone or len(phone) > 32:
            skipped.append(raw)
            continue
        if phone in seen:  # intra-batch duplicate — silently collapse, not reported
            continue
        seen.add(phone)
        member, was_created = Member.objects.get_or_create(phone=phone)
        created += int(was_created)
        member.groups.add(group)  # idempotent — no duplicate through-rows
        linked += 1

    return Response(
        {"group_id": group.group_id, "created": created, "linked": linked, "skipped": skipped},
        status=201,
    )


@api_view(["POST"])
@permission_classes([AllowAny])
def seed_groups(request):
    """Bulk upsert WhatsApp groups: create by group_id, refresh name if it exists."""
    if not _verify(request):
        return Response({"detail": "token tidak valid"}, status=401)
    serializer = WhatsAppSeedGroupsSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    # Collapse intra-batch duplicate group_ids (last name wins); skip blanks.
    wanted = {}
    for item in serializer.validated_data["groups"]:
        gid = item["id"].strip()
        if gid:
            wanted[gid] = (item.get("name") or "").strip()

    existing = {g.group_id: g for g in Group.objects.filter(group_id__in=wanted)}

    to_create, to_update = [], []
    for gid, name in wanted.items():
        group = existing.get(gid)
        if group is None:
            to_create.append(Group(group_id=gid, name=name or None))
        elif name and group.name != name:
            group.name = name
            to_update.append(group)

    Group.objects.bulk_create(to_create)
    if to_update:
        Group.objects.bulk_update(to_update, ["name"])

    return Response({"created": len(to_create), "updated": len(to_update)}, status=201)
