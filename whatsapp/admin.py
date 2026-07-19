from django.contrib import admin

from .models import Group, Member, Message


@admin.register(Group)
class GroupAdmin(admin.ModelAdmin):
    list_display = ["group_id", "name", "created_on"]
    list_editable = ["name"]  # rename straight from the changelist
    search_fields = ["group_id", "name"]


@admin.register(Member)
class MemberAdmin(admin.ModelAdmin):
    list_display = ["phone", "name", "created_on"]
    search_fields = ["phone", "name"]
    filter_horizontal = ["groups"]


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ["member", "group", "created_on"]
    list_filter = ["group"]
    search_fields = ["member__phone", "member__name", "message", "group__group_id", "group__name"]
