from chat.models import Membership , Room
from django.core.exceptions import ValidationError as DjangoValidationError


def is_member( room_id , user_id ):
    return Membership.objects.filter(
        room_id = room_id,
        user_id = user_id ,
        left_at__isnull=True,
        room__deleted_at__isnull=True,

    ).exists()

def get_active_room_or_none(room_id):
    """return an active room , or non for missing/malformed/detelecd room """

    try:
        return Room.objects.filter(pk=room_id).first()
    except (DjangoValidationError , ValueError , TypeError):
        return None

def get_active_membership(room_id , user_id):
    """return active memmbership or non if the user is nhot a member """
    return Membership.objects.filter(
        room_id =room_id,
        user_id = user_id,
        left_at__isnull=True,
    ).first()

def membership_role(room_id, user_id):
    """return the actve role or none if the user is not an active member """
    membership = get_active_membership(room_id ,user_id)
    return membership.role  if membership is not None else None

def is_member(room_id , user_id) -> bool:
    """ return true when user id as an active membership in room_id """
    return get_active_membership(room_id, user_id) is not None

def is_admin(room_id, user_id) -> bool:
    """ return true when user id is active admin of the given room_id"""
    return membership_role(room_id,user_id) == "admin"


def is_creator(room ,user_id) -> bool:
    """ return true when the user id is creator of the room """
    return room is not None and str(room.created_by) == str(user_id)


    




