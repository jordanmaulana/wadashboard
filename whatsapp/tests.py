from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from whatsapp.models import Group, Member, Message

TOKEN = "test-secret"
URL = "/api/v1/whatsapp/members/"


@override_settings(WHATSAPP_WEBHOOK_TOKEN=TOKEN)
class BulkMembersTests(APITestCase):
    def setUp(self):
        self.client = APIClient()

    def _post(self, body, token=TOKEN):
        headers = {"HTTP_X_CALLBACK_TOKEN": token} if token is not None else {}
        return self.client.post(URL, body, format="json", **headers)

    def test_happy_path_counts_dedupe_and_skip_blank(self):
        # DRF CharField trims whitespace by default, so "  " arrives as "" and is
        # echoed back trimmed in `skipped`.
        resp = self._post(
            {"group_id": "g1@g.us", "phones": ["628111", "628222", "628111", "", "  "]}
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(
            resp.json(),
            {"group_id": "g1@g.us", "created": 2, "linked": 2, "skipped": ["", ""]},
        )
        group = Group.objects.get(group_id="g1@g.us")
        self.assertEqual(group.members.count(), 2)
        self.assertEqual(Member.objects.count(), 2)

    def test_idempotent_rerun(self):
        self._post({"group_id": "g1@g.us", "phones": ["628111", "628222"]})
        resp = self._post({"group_id": "g1@g.us", "phones": ["628111", "628222"]})
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertEqual(body["created"], 0)
        self.assertEqual(body["linked"], 2)
        self.assertEqual(Member.objects.count(), 2)
        self.assertEqual(Group.objects.get(group_id="g1@g.us").members.count(), 2)

    def test_over_length_phone_is_skipped_not_500(self):
        long_phone = "6" * 40
        resp = self._post({"group_id": "g1@g.us", "phones": [long_phone, "628111"]})
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertEqual(body["linked"], 1)
        self.assertEqual(body["skipped"], [long_phone])

    def test_bad_token_401(self):
        self.assertEqual(
            self._post({"group_id": "x", "phones": ["1"]}, token="wrong").status_code, 401
        )
        self.assertEqual(
            self._post({"group_id": "x", "phones": ["1"]}, token=None).status_code, 401
        )
        self.assertFalse(Group.objects.exists())

    def test_empty_phones_400(self):
        resp = self._post({"group_id": "x", "phones": []})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(Group.objects.exists())

    def test_missing_group_id_400(self):
        resp = self._post({"phones": ["628111"]})
        self.assertEqual(resp.status_code, 400)


@override_settings(WHATSAPP_WEBHOOK_TOKEN=TOKEN)
class SeedGroupsTests(APITestCase):
    """Bulk group upsert: create new group_ids, refresh names on existing ones.
    Must be idempotent — a rerun with a known group_id updates, never re-inserts."""

    URL = "/api/v1/whatsapp/groups/"

    def setUp(self):
        self.client = APIClient()

    def _post(self, body, token=TOKEN):
        headers = {"HTTP_X_CALLBACK_TOKEN": token} if token is not None else {}
        return self.client.post(self.URL, body, format="json", **headers)

    def test_happy_path_creates_groups(self):
        resp = self._post({"groups": [{"id": "g1@g.us", "name": "One"}, {"id": "g2@g.us"}]})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json(), {"created": 2, "updated": 0})
        self.assertEqual(Group.objects.get(group_id="g1@g.us").name, "One")
        self.assertIsNone(Group.objects.get(group_id="g2@g.us").name)

    def test_idempotent_rerun_updates_not_reinserts(self):
        self._post({"groups": [{"id": "g1@g.us", "name": "Old"}]})
        resp = self._post({"groups": [{"id": "g1@g.us", "name": "New"}]})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json(), {"created": 0, "updated": 1})
        self.assertEqual(Group.objects.filter(group_id="g1@g.us").count(), 1)
        self.assertEqual(Group.objects.get(group_id="g1@g.us").name, "New")

    def test_rerun_same_name_no_op(self):
        self._post({"groups": [{"id": "g1@g.us", "name": "Same"}]})
        resp = self._post({"groups": [{"id": "g1@g.us", "name": "Same"}]})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json(), {"created": 0, "updated": 0})
        self.assertEqual(Group.objects.filter(group_id="g1@g.us").count(), 1)

    def test_bad_token_401(self):
        self.assertEqual(self._post({"groups": [{"id": "x"}]}, token="wrong").status_code, 401)
        self.assertEqual(self._post({"groups": [{"id": "x"}]}, token=None).status_code, 401)
        self.assertFalse(Group.objects.exists())

    def test_empty_groups_400(self):
        resp = self._post({"groups": []})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(Group.objects.exists())


@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
)
class WhatsAppActivityViewTests(TestCase):
    """The staff dashboard: default 30-day window, roster-based inactive count,
    group scoping, and superuser gating."""

    URL = "/whatsapp/"

    def setUp(self):
        self.staff = User.objects.create_superuser("staff", "staff@x.local", "pw")
        self.client.force_login(self.staff)

    def _member(self, phone, *groups):
        m = Member.objects.create(phone=phone)
        m.groups.add(*groups)
        return m

    def _msg(self, member, group, when):
        """Create a message, then backdate it (created_on is auto_now_add)."""
        msg = Message.objects.create(member=member, group=group, message="hi")
        Message.objects.filter(id=msg.id).update(created_on=when)
        return msg

    def test_default_window_is_last_30_days(self):
        g = Group.objects.create(group_id="g@x")
        mem = self._member("628111", g)
        now = timezone.now()
        self._msg(mem, g, now - timedelta(days=5))  # inside window
        self._msg(mem, g, now - timedelta(days=40))  # outside window

        resp = self.client.get(self.URL)
        self.assertEqual(resp.status_code, 200)
        today = timezone.localdate()
        self.assertEqual(resp.context["start"], (today - timedelta(days=29)).isoformat())
        self.assertEqual(resp.context["end"], today.isoformat())
        self.assertEqual(resp.context["total"], 1)  # 40-day-old message excluded

    def test_inactive_equals_roster_minus_active(self):
        g = Group.objects.create(group_id="g@x")
        active = self._member("628aaa", g)
        self._member("628bbb", g)  # rostered but silent
        self._msg(active, g, timezone.now() - timedelta(days=2))

        ctx = self.client.get(self.URL).context
        self.assertEqual(ctx["roster_count"], 2)
        self.assertEqual(ctx["active_count"], 1)
        self.assertEqual(ctx["inactive_count"], 1)
        self.assertEqual([m.phone for m in ctx["inactive_members"]], ["628bbb"])
        # "Diam (0)" segment mirrors the inactive count.
        self.assertEqual(ctx["segments"][0]["value"], 1)

    def test_group_filter_scopes_metrics(self):
        g1 = Group.objects.create(group_id="g1@x")
        g2 = Group.objects.create(group_id="g2@x")
        m1 = self._member("628111", g1)
        m2 = self._member("628222", g2)
        self._msg(m1, g1, timezone.now() - timedelta(days=1))
        self._msg(m2, g2, timezone.now() - timedelta(days=1))

        ctx = self.client.get(self.URL, {"group": "g1@x"}).context
        self.assertEqual(ctx["total"], 1)
        self.assertEqual(ctx["roster_count"], 1)
        self.assertEqual(ctx["active_count"], 1)
        self.assertEqual(ctx["inactive_count"], 0)

    def test_new_vs_returning_split(self):
        g = Group.objects.create(group_id="g@x")
        newcomer = self._member("628new", g)
        veteran = self._member("628old", g)
        now = timezone.now()
        self._msg(newcomer, g, now - timedelta(days=3))  # first ever, inside window
        self._msg(veteran, g, now - timedelta(days=90))  # first ever, before window
        self._msg(veteran, g, now - timedelta(days=3))  # active again inside window

        ctx = self.client.get(self.URL).context
        self.assertEqual(ctx["active_count"], 2)
        self.assertEqual(ctx["new_count"], 1)
        self.assertEqual(ctx["returning_count"], 1)

    def test_non_superuser_redirected_to_login(self):
        self.client.logout()
        resp = self.client.get(self.URL)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp["Location"])
