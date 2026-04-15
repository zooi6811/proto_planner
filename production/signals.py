from django.db.models.signals import post_save
from django.dispatch import receiver
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from .models import ExtrusionLog, CuttingLog, PackingLog, JobOrder, CuttingSession, ExtrusionSession

def trigger_live_update():
    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        "live_factory", {"type": "factory_update"}
    )

# Listen for any new logs or status changes
@receiver(post_save, sender=ExtrusionLog)
@receiver(post_save, sender=CuttingLog)
@receiver(post_save, sender=PackingLog)
@receiver(post_save, sender=JobOrder)
@receiver(post_save, sender=ExtrusionSession)
@receiver(post_save, sender=CuttingSession)
def broadcast_factory_change(sender, instance, **kwargs):
    trigger_live_update()