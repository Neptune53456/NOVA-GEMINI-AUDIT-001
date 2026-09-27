"""HTTP-only conversation routing; business behavior lives in the service."""

import json

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse

from .conversation_service import ConversationBusy, ConversationLimitReached, ConversationNotFound, ConversationService, EngineTimedOut, GenerationNotFound, MAX_MESSAGE_LENGTH
from .engine_adapter import EngineBusyError, EngineUnavailableError
from .schemas import (CancelGenerationResponse, ConfirmationDecisionRequest, ConfirmationDecisionResponse,
                      CreateConversationRequest, ConversationDetail, ConversationSummary, SendMessageRequest, SendMessageResponse)


def build_router(service: ConversationService) -> APIRouter:
    router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])

    @router.post("", response_model=ConversationSummary, status_code=status.HTTP_201_CREATED)
    def create(request: CreateConversationRequest) -> ConversationSummary:
        try: return service.create(request.mode)
        except ConversationLimitReached as error: raise HTTPException(409, "Limite de conversations atteinte.") from error

    @router.get("/{conversation_id}", response_model=ConversationDetail)
    def get(conversation_id: str) -> ConversationDetail:
        try: return service.get(conversation_id)
        except ConversationNotFound as error: raise HTTPException(404, "Conversation inconnue.") from error

    @router.post("/{conversation_id}/messages", response_model=SendMessageResponse)
    async def send(conversation_id: str, request: SendMessageRequest) -> SendMessageResponse:
        content = request.content.strip()
        if not content: raise HTTPException(422, "Le message est vide.")
        if len(content) > MAX_MESSAGE_LENGTH: raise HTTPException(422, f"Le message dépasse {MAX_MESSAGE_LENGTH} caractères.")
        try: return await service.send(conversation_id, content)
        except ConversationNotFound as error: raise HTTPException(404, "Conversation inconnue.") from error
        except (ConversationBusy, ConversationLimitReached) as error: raise HTTPException(409, "Conversation occupée ou limite de messages atteinte.") from error
        except EngineTimedOut as error: raise HTTPException(504, "Le moteur Nova n’a pas répondu à temps.") from error
        except EngineBusyError as error: raise HTTPException(503, "Moteur Nova occupé.") from error
        except EngineUnavailableError as error: raise HTTPException(503, "Moteur Nova indisponible.") from error

    @router.post("/{conversation_id}/messages/stream")
    async def stream(conversation_id: str, body: SendMessageRequest, request: Request) -> StreamingResponse:
        content = body.content.strip()
        if not content: raise HTTPException(422, "Le message est vide.")
        if len(content) > MAX_MESSAGE_LENGTH: raise HTTPException(422, f"Le message dépasse {MAX_MESSAGE_LENGTH} caractères.")
        try: generation_id = service.start_generation(conversation_id, content)
        except ConversationNotFound as error: raise HTTPException(404, "Conversation inconnue.") from error
        except (ConversationBusy, ConversationLimitReached) as error: raise HTTPException(409, "Conversation occupée ou limite de messages atteinte.") from error
        except EngineBusyError as error: raise HTTPException(503, "Moteur Nova occupé.") from error

        async def events():
            try:
                async for event in service.stream(generation_id):
                    if await request.is_disconnected():
                        service.cancel(conversation_id, generation_id)
                        break
                    yield f"event: {event['event']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            except (GenerationNotFound, ConversationBusy):
                return

        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @router.post("/{conversation_id}/generations/{generation_id}/cancel", response_model=CancelGenerationResponse)
    def cancel(conversation_id: str, generation_id: str) -> CancelGenerationResponse:
        try: service.cancel(conversation_id, generation_id)
        except GenerationNotFound as error: raise HTTPException(404, "Génération inconnue.") from error
        return CancelGenerationResponse(generation_id=generation_id)

    @router.post("/{conversation_id}/confirmations", response_model=ConfirmationDecisionResponse)
    async def decide_confirmation(conversation_id: str, body: ConfirmationDecisionRequest) -> ConfirmationDecisionResponse:
        try:
            decision, message = await service.decide_confirmation(conversation_id, body.token, body.approved)
        except GenerationNotFound as error:
            raise HTTPException(409, "Confirmation invalide ou expirée.") from error
        except EngineUnavailableError as error:
            raise HTTPException(503, "Moteur Nova indisponible.") from error
        return ConfirmationDecisionResponse(status=decision, assistant_message=message)

    @router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete(conversation_id: str) -> Response:
        try: service.delete(conversation_id)
        except ConversationNotFound as error: raise HTTPException(404, "Conversation inconnue.") from error
        except ConversationBusy as error: raise HTTPException(409, "Une génération est en cours.") from error
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router
