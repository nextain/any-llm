"""Live API WebSocket proxy — bidirectional voice via Gemini Live API.

Routes voice/text through Gemini Live API using Vertex AI ADC.
Client authenticates with labKey (API key), gateway proxies to Gemini.

Protocol:
  1. Client sends setup message with apiKey, voice, systemInstruction, tools
  2. Gateway verifies API key, creates Gemini Live session
  3. Bidirectional relay: client ↔ gateway ↔ Gemini Live
  4. On disconnect, usage is logged and credits charged
"""

import asyncio
import base64
import json
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from any_llm.gateway.auth.dependencies import get_config
from any_llm.gateway.auth.models import hash_key
from any_llm.gateway.db import APIKey, UsageLog, User, get_db
from any_llm.gateway.log_config import logger
from any_llm.gateway.routes.utils import (
    charge_user_credits,
    get_user_credits_per_usd,
    validate_user_credit,
)

router = APIRouter(tags=["live"])

DEFAULT_LIVE_MODEL = "gemini-2.0-flash-live-001"

# Gemini Live pricing (USD per million tokens) — same as gemini-2.0-flash
_LIVE_INPUT_PRICE = 0.10
_LIVE_OUTPUT_PRICE = 0.40


def _verify_api_key_ws(db, token_str: str):
    """Verify API key for WebSocket (can't use FastAPI Depends).

    Returns:
        (APIKey, user_id) or (None, None) on failure.
    """
    if not token_str:
        return None, None

    token = token_str.removeprefix("Bearer ")

    try:
        key_hash = hash_key(token)
    except ValueError:
        return None, None

    api_key = db.query(APIKey).filter(APIKey.key_hash == key_hash).first()
    if not api_key or not api_key.is_active:
        return None, None

    if api_key.expires_at and api_key.expires_at < datetime.now(UTC).replace(tzinfo=None):
        return None, None

    api_key.last_used_at = datetime.now(UTC).replace(tzinfo=None)
    db.commit()

    user_id = str(api_key.user_id) if api_key.user_id else None
    return api_key, user_id


def _build_tools(tools_list):
    """Convert client tool declarations to google-genai FunctionDeclaration."""
    from google.genai import types

    if not tools_list:
        return None

    declarations = []
    for tool in tools_list:
        declarations.append(
            types.FunctionDeclaration(
                name=tool.get("name", ""),
                description=tool.get("description", ""),
                parameters=tool.get("parameters"),
            ),
        )
    return [types.Tool(function_declarations=declarations)]


def _serialize_response(response) -> dict | None:
    """Convert google-genai LiveServerMessage to JSON dict for the client."""
    sc = getattr(response, "server_content", None)
    if sc:
        out = {}

        mt = getattr(sc, "model_turn", None)
        if mt and mt.parts:
            parts = []
            for part in mt.parts:
                inline = getattr(part, "inline_data", None)
                if inline:
                    raw = inline.data
                    b64 = base64.b64encode(raw).decode("ascii") if isinstance(raw, bytes) else str(raw)
                    parts.append({"inlineData": {"mimeType": inline.mime_type or "audio/pcm;rate=24000", "data": b64}})
                elif getattr(part, "text", None):
                    parts.append({"text": part.text})
            if parts:
                out["modelTurn"] = {"parts": parts}

        itx = getattr(sc, "input_transcription", None)
        if itx and getattr(itx, "text", None):
            out["inputTranscription"] = {"text": itx.text}

        otx = getattr(sc, "output_transcription", None)
        if otx and getattr(otx, "text", None):
            out["outputTranscription"] = {"text": otx.text}

        if getattr(sc, "turn_complete", False):
            out["turnComplete"] = True

        if getattr(sc, "interrupted", False):
            out["interrupted"] = True

        if out:
            return {"serverContent": out}

    tc = getattr(response, "tool_call", None)
    if tc and tc.function_calls:
        calls = [
            {"id": fc.id, "name": fc.name, "args": dict(fc.args) if fc.args else {}}
            for fc in tc.function_calls
        ]
        return {"toolCall": {"functionCalls": calls}}

    return None


def _log_live_usage(db, api_key_obj, user_id, input_tokens, output_tokens):
    """Log usage and charge credits for a completed Live session."""
    total = input_tokens + output_tokens
    if total <= 0:
        return

    cost_usd = (input_tokens / 1_000_000) * _LIVE_INPUT_PRICE + (output_tokens / 1_000_000) * _LIVE_OUTPUT_PRICE

    usage_log = UsageLog(
        id=str(uuid.uuid4()),
        api_key_id=api_key_obj.id if api_key_obj else None,
        user_id=user_id,
        timestamp=datetime.now(UTC).replace(tzinfo=None),
        model=DEFAULT_LIVE_MODEL,
        provider="vertexai",
        endpoint="/v1/live",
        status="success",
        prompt_tokens=input_tokens,
        completion_tokens=output_tokens,
        total_tokens=total,
        cached_tokens=0,
        cost=cost_usd,
    )
    db.add(usage_log)
    try:
        db.commit()
    except Exception as exc:
        logger.error("Failed to log Live usage: %s", str(exc))
        db.rollback()
        return

    if cost_usd > 0:
        try:
            credits_per_usd = get_user_credits_per_usd(db, user_id)
            charge_user_credits(
                db,
                user_id=user_id,
                cost_usd=cost_usd,
                credits_per_usd=credits_per_usd,
                model_key=f"vertexai:{DEFAULT_LIVE_MODEL}",
                usage_id=usage_log.id,
            )
        except Exception as e:
            logger.warning("Live credit charge failed", extra={"user_id": user_id, "error": str(e)})

    try:
        user = db.query(User).filter(User.user_id == user_id).first()
        if user and cost_usd > 0:
            user.spend = (user.spend or 0) + cost_usd
            db.commit()
    except Exception:
        db.rollback()


@router.websocket("/v1/live")
async def live_proxy(ws: WebSocket):
    """Bidirectional WebSocket proxy to Gemini Live API."""
    await ws.accept()

    db = next(get_db())
    api_key_obj = None
    user_id = None
    total_input_tokens = 0
    total_output_tokens = 0

    try:
        # --- 1. Setup: auth + config ---
        raw = await asyncio.wait_for(ws.receive_text(), timeout=30)
        msg = json.loads(raw)
        setup = msg.get("setup", {})

        api_key_obj, user_id = _verify_api_key_ws(db, setup.get("apiKey", ""))
        if not api_key_obj:
            await ws.send_json({"error": {"message": "Invalid API key"}})
            await ws.close(code=4001)
            return

        try:
            validate_user_credit(db, user_id)
        except Exception:
            await ws.send_json({"error": {"message": "Insufficient credits"}})
            await ws.close(code=4003)
            return

        # --- 2. Create Gemini Live session ---
        config = get_config()
        vertex = config.providers.get("vertexai", {})
        project = vertex.get("project")
        # Live API may not support "global" — fall back to us-central1
        location = vertex.get("location", "us-central1")
        if location == "global":
            location = "us-central1"

        model = setup.get("model", DEFAULT_LIVE_MODEL)

        from google import genai
        from google.genai import types

        client = genai.Client(vertexai=True, project=project, location=location)

        live_config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=setup.get("voice", "Puck"),
                    ),
                ),
            ),
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
        )

        si = setup.get("systemInstruction")
        if si:
            live_config.system_instruction = types.Content(parts=[types.Part(text=si)])

        tools = _build_tools(setup.get("tools"))
        if tools:
            live_config.tools = tools

        async with client.aio.live.connect(model=model, config=live_config) as session:
            await ws.send_json({"setupComplete": {}})

            # --- 3. Bidirectional relay ---
            async def client_to_gemini():
                try:
                    while True:
                        data = json.loads(await ws.receive_text())

                        if "realtimeInput" in data:
                            for chunk in data["realtimeInput"].get("mediaChunks", []):
                                pcm = base64.b64decode(chunk["data"])
                                await session.send_realtime_input(
                                    audio=types.Blob(
                                        data=pcm,
                                        mime_type=chunk.get("mimeType", "audio/pcm;rate=16000"),
                                    ),
                                )

                        elif "clientContent" in data:
                            cc = data["clientContent"]
                            turns = []
                            for turn in cc.get("turns", []):
                                parts = [types.Part(text=p["text"]) for p in turn.get("parts", []) if "text" in p]
                                turns.append(types.Content(role=turn.get("role", "user"), parts=parts))
                            await session.send_client_content(
                                turns=turns,
                                turn_complete=cc.get("turnComplete", True),
                            )

                        elif "toolResponse" in data:
                            tr = data["toolResponse"]
                            responses = [
                                types.FunctionResponse(id=fr["id"], response=fr.get("response", {}))
                                for fr in tr.get("functionResponses", [])
                            ]
                            await session.send_tool_response(function_responses=responses)

                except (WebSocketDisconnect, asyncio.CancelledError):
                    pass
                except Exception as e:
                    logger.warning("client_to_gemini error: %s", str(e))

            async def gemini_to_client():
                nonlocal total_input_tokens, total_output_tokens
                try:
                    while True:  # multi-turn: session.receive() ends per turn
                        async for resp in session.receive():
                            serialized = _serialize_response(resp)
                            if serialized:
                                await ws.send_json(serialized)

                            um = getattr(resp, "usage_metadata", None)
                            if um:
                                total_input_tokens = getattr(um, "prompt_token_count", 0) or 0
                                total_output_tokens = getattr(um, "candidates_token_count", 0) or 0
                except (WebSocketDisconnect, asyncio.CancelledError):
                    pass
                except Exception as e:
                    logger.warning("gemini_to_client error: %s", str(e))

            tasks = [asyncio.create_task(client_to_gemini()), asyncio.create_task(gemini_to_client())]
            _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass

    except WebSocketDisconnect:
        logger.info("Live session disconnected", extra={"user_id": user_id})
    except Exception as e:
        logger.error("Live session error: %s", str(e), extra={"user_id": user_id})
        try:
            await ws.send_json({"error": {"message": str(e)}})
            await ws.close(code=4500)
        except Exception:
            pass
    finally:
        # --- 4. Log usage on session end ---
        if user_id:
            try:
                _log_live_usage(db, api_key_obj, user_id, total_input_tokens, total_output_tokens)
            except Exception as e:
                logger.error("Failed to log live usage: %s", str(e))
        db.close()
