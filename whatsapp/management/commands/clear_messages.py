"""Delete all WhatsApp data: Message, Member and Group rows.

uv run manage.py clear_messages
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from whatsapp.models import Group, Member, Message


class Command(BaseCommand):
    help = "Delete all WhatsApp messages, members and groups."

    def handle(self, *args, **options):
        with transaction.atomic():
            # Messages first: Member/Group are PROTECT targets, so they cannot be
            # deleted while any Message still references them.
            messages, _ = Message.objects.all().delete()
            members, _ = Member.objects.all().delete()
            groups, _ = Group.objects.all().delete()
        self.stdout.write(
            self.style.SUCCESS(f"Cleared {messages} messages, {members} members, {groups} groups.")
        )
