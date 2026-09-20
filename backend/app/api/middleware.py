"""Attach a request id to every request (honouring an inbound X-Request-ID)
and echo it on the response so logs and clients can be correlated."""

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.context import bind_request_id, new_request_id, request_id_var

HEADER = b"x-request-id"


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        inbound = dict(scope.get("headers") or []).get(HEADER)
        request_id = inbound.decode("latin-1") if inbound else new_request_id()
        token = bind_request_id(request_id)

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                headers.append((HEADER, request_id.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            request_id_var.reset(token)
