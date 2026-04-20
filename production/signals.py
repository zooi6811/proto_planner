from django.db.models.signals import post_save
from django.dispatch import receiver
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from django.db import transaction
from .models import JobOrder

def trigger_live_update():
    """
    Broadcasts the update, but ONLY after the current database transaction is fully committed.
    This prevents the dreaded 'stale read' race condition.
    """
    def broadcast():
        channel_layer = get_channel_layer()
        async_to_sync(channel_layer.group_send)(
            "live_factory", 
            {
                "type": "factory_update",
                "message_type": "softRefresh"
            }
        )
    
    transaction.on_commit(broadcast)

# Example Integration: We only track the JobOrder model for this first step.
@receiver(post_save, sender=JobOrder)
def broadcast_job_change(sender, instance, **kwargs):
    trigger_live_update()