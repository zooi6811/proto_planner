from django.db.models.signals import post_save
from django.dispatch import Signal, receiver
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from django.db import transaction
from .models import JobOrder, ExtrusionLog, CuttingLog, ExtrusionSession, CuttingSession
import json
from decimal import Decimal

yield_adapted = Signal()

def trigger_live_update(recipe_id, stage, new_wastage):
    """
    Pushes the updated yield via WebSockets/Channels to the Control Tower HTMX frontend.
    """
    channel_layer = get_channel_layer()
    if channel_layer:
        async_to_sync(channel_layer.group_send)(
            "control_tower_updates",
            {
                "type": "yield_update_message",
                "payload": json.dumps({
                    "recipe_id": recipe_id,
                    "stage": stage,
                    "new_wastage_rate": str(round(new_wastage, 4)),
                    "new_yield_pct": str(round((Decimal('1.0000') - new_wastage) * 100, 2))
                })
            }
        )

@receiver(yield_adapted)
def broadcast_yield_adaptation(sender, recipe_id, stage, new_wastage, **kwargs):
    """
    Catches the yield update and defers the live broadcast until 
    the database transaction has successfully committed. This prevents
    the frontend from fetching stale data if the transaction is slightly delayed.
    """
    transaction.on_commit(lambda: trigger_live_update(recipe_id, stage, new_wastage))
# Example Integration: We only track the JobOrder model for this first step.
@receiver(post_save, sender=JobOrder)
@receiver(post_save, sender=ExtrusionSession)
@receiver(post_save, sender=CuttingSession)
@receiver(post_save, sender=ExtrusionLog)
@receiver(post_save, sender=CuttingLog)
def broadcast_job_change(sender, instance, **kwargs):
    trigger_live_update()