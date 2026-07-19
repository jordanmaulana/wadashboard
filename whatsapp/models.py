from django.db import models

from core.models import BaseModel


class Group(BaseModel):
    """A WhatsApp group chat.

    The relay webhook supplies only `group_id`; the human-readable `name` is
    unknown at ingest time and is set manually by staff (Django admin or the
    dashboard inline rename).
    """

    group_id = models.CharField(max_length=128, unique=True)
    name = models.CharField(max_length=255, blank=True, null=True)

    class Meta:
        ordering = ["name", "group_id"]

    def __str__(self):
        return self.name or self.group_id


class Member(BaseModel):
    """A WhatsApp sender, identified by phone number.

    One identity per phone (`phone` stays globally unique). `groups` tracks every
    group the member has been seen in — a person in two groups is still one Member.
    `name` tracks the member's latest display name — refreshed by the webhook on
    every message, so it is always current (no per-message name lookup needed).
    Members are not necessarily registered app users, so this is standalone.
    """

    phone = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=255, blank=True)
    groups = models.ManyToManyField(Group, related_name="members", blank=True)

    class Meta:
        ordering = ["name", "phone"]

    def __str__(self):
        return self.name or self.phone


class Message(BaseModel):
    """A single message logged from a WhatsApp group chat.

    `created_on` (from BaseModel) is the log timestamp used for the dashboard's
    date filter.
    """

    message = models.TextField(blank=True)  # media messages may carry no text
    member = models.ForeignKey(Member, related_name="messages", on_delete=models.PROTECT)
    group = models.ForeignKey(Group, related_name="messages", on_delete=models.PROTECT)

    class Meta:
        ordering = ["-created_on"]
        indexes = [models.Index(fields=["created_on"])]

    def __str__(self):
        return f"{self.member}: {self.message[:40]}"
