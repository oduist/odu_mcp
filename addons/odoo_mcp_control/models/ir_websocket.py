from odoo import models


class IrWebsocket(models.AbstractModel):
    _inherit = "ir.websocket"

    def _build_bus_channel_list(self, channels):
        event_channel = self.env.context.get("odoo_mcp_event_channel")
        if event_channel:
            return [event_channel]
        return super()._build_bus_channel_list(channels)
