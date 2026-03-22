from enum import Enum

from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand
import structlog

from myapp.custom_groups import (
    QRManager,
    QRGuard,
    UserManager,
    SeniorUserManager,
    CanteenManager,
    CanteenEmployee,
    CanteenAdminManager,
    DispatchAdminManager,
    DispatchSuperViewer
)


logger = structlog.get_logger(__name__)


class PermissionType(str, Enum):
    VIEW = 'view'
    DELETE = 'delete'
    CHANGE = 'change'
    ADD = 'add'


ALL_PERMISSIONS = (PermissionType.VIEW, PermissionType.DELETE, PermissionType.CHANGE, PermissionType.ADD)

roles = {
    QRManager: {
        'round': [PermissionType.VIEW],
        'visit': [PermissionType.VIEW],
        'message': ALL_PERMISSIONS,
        'point': ALL_PERMISSIONS,
        'guard': ALL_PERMISSIONS,
    },
    QRGuard: {
        # no access to admin panel
    },
    UserManager: {
        'user': ALL_PERMISSIONS,
        'device': ALL_PERMISSIONS,
    },
    SeniorUserManager: {
        'user': ALL_PERMISSIONS,
        'device': ALL_PERMISSIONS,
    },
    CanteenManager: {
        'dish': ALL_PERMISSIONS,
        'alloweddish': ALL_PERMISSIONS,
        'order': [PermissionType.VIEW],
        'feedback': [PermissionType.VIEW]
    },
    CanteenAdminManager: {
        'dish': ALL_PERMISSIONS,
        'alloweddish': ALL_PERMISSIONS,
        'order': ALL_PERMISSIONS,
        'feedback': [PermissionType.VIEW]
    },
    CanteenEmployee: {
        'dish': [PermissionType.VIEW],
        'alloweddish': [PermissionType.VIEW],
        'order': ALL_PERMISSIONS,
        'feedback': [PermissionType.VIEW, PermissionType.ADD]
    },
    DispatchAdminManager: {
        'incident': ALL_PERMISSIONS,
        'duty': ALL_PERMISSIONS,
        'dutyrole': ALL_PERMISSIONS,
        'dutypoint': ALL_PERMISSIONS,
        'exploitationrole': ALL_PERMISSIONS,
        'incidentmessage': ALL_PERMISSIONS,
        'textmessage': ALL_PERMISSIONS,
        'photomessage': ALL_PERMISSIONS,
        'videomessage': ALL_PERMISSIONS,
        'audiomessage': ALL_PERMISSIONS,
    },
    DispatchSuperViewer: {
        'incident': [PermissionType.VIEW],
        'duty': [PermissionType.VIEW],
        'dutyrole': [PermissionType.VIEW],
        'dutypoint': [PermissionType.VIEW],
        'exploitationrole': [PermissionType.VIEW],
        'incidentmessage': [PermissionType.VIEW],
        'textmessage': [PermissionType.VIEW],
        'photomessage': [PermissionType.VIEW],
        'videomessage': [PermissionType.VIEW],
        'audiomessage': [PermissionType.VIEW],
    }
}


class Command(BaseCommand):
    help = 'Create Managers group with permissions for myapp'

    def handle(self, *args, **options):
        for custom_group in roles.keys():
            group, _ = Group.objects.get_or_create(name=custom_group.name)

            group.permissions.clear()

            for model_name in roles[custom_group].keys():
                content_type = ContentType.objects.get(
                    model=model_name) if model_name != 'user' else ContentType.objects.get(model=model_name,
                                                                                           app_label='users')

                for permission in ALL_PERMISSIONS:
                    codename = f'{permission.value}_{model_name}'

                    logger.info(
                        "group_permission_sync_checked",
                        group_name=custom_group.name,
                        codename=codename,
                        content_type_id=content_type.id,
                    )
                    p, _ = Permission.objects.get_or_create(codename=codename, content_type=content_type)
                    if permission in roles[custom_group][model_name]:
                        if not group.permissions.filter(codename=p.codename).exists():
                            group.permissions.add(p)
                            self.stdout.write(f'Permission "{codename}" added to group "{custom_group.name}"')
                    else:
                        if group.permissions.filter(codename=p.codename).exists():
                            group.permissions.remove(p)
                            self.stdout.write(f'Permission "{codename}" removed from group "{custom_group.name}"')

            self.stdout.write(f'Permissions added to group "{custom_group.name}"')
