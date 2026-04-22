from django.db.models.signals import post_save
from django.dispatch import Signal, receiver
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from django.db import transaction
from .models import JobOrder, ExtrusionLog, CuttingLog, ExtrusionSession, CuttingSession
import json
from decimal import Decimal

yield_adapted = Signal()

def trigger_yield_update(recipe_id, stage, new_wastage):
    channel_layer = get_channel_layer()
    if channel_layer:
        async_to_sync(channel_layer.group_send)(
            "live_factory",  # FIXED: Pointing to the correct group
            {
                "type": "yield_update_message",
                "payload": json.dumps({
                    "type": "yield_update",
                    "recipe_id": recipe_id,
                    "stage": stage,
                    "new_wastage_rate": str(round(new_wastage, 4)),
                    "new_yield_pct": str(round((Decimal('1.0000') - new_wastage) * 100, 2))
                })
            }
        )

def trigger_supervisor_alert(jo_number, variance_type, message):
    channel_layer = get_channel_layer()
    if channel_layer:
        async_to_sync(channel_layer.group_send)(
            "live_factory",  # FIXED: Pointing to the correct group
            {
                "type": "supervisor_alert_message",
                "payload": json.dumps({
                    "type": "supervisor_alert",
                    "jo_number": jo_number,
                    "variance_type": variance_type,
                    "message": message
                })
            }
        )

@receiver(yield_adapted)
def broadcast_yield_adaptation(sender, recipe_id, stage, new_wastage, **kwargs):
    transaction.on_commit(lambda: trigger_yield_update(recipe_id, stage, new_wastage))


def trigger_live_update():
    """
    Broadcasts the update, but ONLY after the current database transaction is fully committed.
    """
    def broadcast():
        channel_layer = get_channel_layer()
        if channel_layer:
            async_to_sync(channel_layer.group_send)(
                "live_factory",  # FIXED: Restored to original correct group
                {
                    "type": "factory_update",
                    "message_type": "softRefresh"
                }
            )
    transaction.on_commit(broadcast)


@receiver(post_save, sender=JobOrder)
@receiver(post_save, sender=ExtrusionSession)
@receiver(post_save, sender=CuttingSession)
@receiver(post_save, sender=ExtrusionLog)
@receiver(post_save, sender=CuttingLog)
def broadcast_job_change(sender, instance, **kwargs):
    trigger_live_update()