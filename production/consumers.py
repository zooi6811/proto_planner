import json
from channels.generic.websocket import AsyncWebsocketConsumer

class LiveFactoryConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.group_name = 'live_factory'

        # Join the global factory group
        await self.channel_layer.group_add(
            self.group_name,
            self.channel_name
        )
        await self.accept()

    async def disconnect(self, close_code):
        # Leave the group on disconnect
        await self.channel_layer.group_discard(
            self.group_name,
            self.channel_name
        )

    # Receive message from room group
    async def factory_update(self, event):
        message_type = event.get('message_type', 'softRefresh')
        
        # Send a minimal JSON payload to the client
        await self.send(text_data=json.dumps({
            'type': message_type
        }))