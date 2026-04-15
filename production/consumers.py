import json
from channels.generic.websocket import AsyncWebsocketConsumer

class ProductionSignalConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        await self.channel_layer.group_add("live_factory", self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard("live_factory", self.channel_name)

    async def factory_update(self, event):
        # We removed the <script> tag. Now we just send a harmless ping to trigger HTMX.
        await self.send(text_data='<div id="ws-bridge" hx-swap-oob="true"></div>')