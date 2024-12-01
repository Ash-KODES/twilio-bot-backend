import os
import json
import base64
import asyncio
import websockets
import requests
from fastapi import FastAPI, WebSocket, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.websockets import WebSocketDisconnect
from twilio.twiml.voice_response import VoiceResponse, Connect, Say, Stream
from dotenv import load_dotenv
from firebase_handler import db, bucket
import ffmpeg
from pydub import AudioSegment
from pydub.silence import detect_nonsilent
import audioop
import wave

import argparse

client = os.getenv("CLIENT", "bruntwork")
load_dotenv()


async def fetch_active_prompt():
    prompt_doc = db.collection("prompts").document("active").get()
    if prompt_doc.exists:
        return prompt_doc.to_dict().get("prompt", "Default system prompt.")
    return "Default system prompt."


async def store_phone_call(phone_call_data):
    call_id = db.collection("phone_calls").add(phone_call_data)[1].id
    return call_id


def upload_audio_to_storage(local_file_path, destination_blob_name):
    blob = bucket.blob(destination_blob_name)
    blob.upload_from_filename(local_file_path)
    return blob.public_url


# Configuration
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
# PORT = int(os.getenv('PORT', 5050))
# Always start with: Hello! I’m Paul, Bruntworks’ customer service representative. Are you calling to hire, speak with your Customer Success Manager, or inquire about a job opportunity with Bruntwork?
#
PORT = 8082
# SYSTEM_MESSAGE =
SYSTEM_MESSAGE = ""

# ash, ballad, coral, sage and verse
VOICE = 'coral'
LOG_EVENT_TYPES = [
    'error', 'response.content.done', 'rate_limits.updated',
    'response.done', 'input_audio_buffer.committed',
    'input_audio_buffer.speech_stopped', 'input_audio_buffer.speech_started',
    'session.created', 'conversation.item.input_audio_transcription.completed'
]
SHOW_TIMING_MATH = False

app = FastAPI()

if not OPENAI_API_KEY:
    raise ValueError('Missing the OpenAI API key. Please set it in the .env file.')

import wave
from collections import defaultdict

# Dictionary to manage WAV files for each call
merged_audio_files = {}
client_audio_files = {}
bot_audio_files = {}

def initialize_wave_file(filename):
    """Initialize a WAV file for writing."""
    wf = wave.open(filename, 'wb')
    wf.setnchannels(1)  # Mono audio
    wf.setsampwidth(2)  # 16 bit audio
    wf.setframerate(8000)  # 8 kHz sample rate
    return wf


def remove_silence(input_file, output_file, silence_thresh=-60, min_silence_len=1000, buffer_ms=700):
    """
    Remove silence from the middle of an audio file using pydub.
    
    :param input_file: Path to the input audio file.
    :param output_file: Path to save the output audio without silence.
    :param silence_thresh: Silence threshold in dBFS (decibels relative to full scale).
    :param min_silence_len: Minimum duration of silence to be removed, in milliseconds.
    """
    # Load the audio file
    audio = AudioSegment.from_file(input_file, format="wav")

    # Detect non-silent intervals
    nonsilent_ranges = detect_nonsilent(audio, min_silence_len=min_silence_len, silence_thresh=silence_thresh)

    # Concatenate non-silent segments
    nonsilent_audio = AudioSegment.silent(duration=0)  # Start with an empty audio segment
    for start, end in nonsilent_ranges:
        nonsilent_audio += audio[start:end] + AudioSegment.silent(duration=buffer_ms)

    # Export the processed audio
    nonsilent_audio.export(output_file, format="wav", parameters=["-ar", str(audio.frame_rate)])
    print(f"Silence removed. Output saved to {output_file}")

@app.get("/", response_class=JSONResponse)
async def index_page():
    return {"message": "Twilio Media Stream Server is running!"}

@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    """Handle incoming call and return TwiML response to connect to Media Stream."""

    form_data = await request.form()  # Extract form data sent by Twilio
    caller_phone_number = form_data.get("From")  # Get the caller's phone number

    active_prompt = await fetch_active_prompt()
    print(f"Active Prompt: {active_prompt}")
    SYSTEM_MESSAGE = active_prompt

    response = VoiceResponse()
    # <Say> punctuation to improve text-to-speech flow
    # response.say("Please wait while we connect your call")
    response.pause(length=1)
    # response.say("O.K. you can start talking!")
    host = request.url.hostname
    print(f"Host URL is {host}")

    connect = Connect()
    connect.stream(url=f'wss://{host}/media-stream')
    response.append(connect)
    return HTMLResponse(content=str(response), media_type="application/xml")

@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    """Handle WebSocket connections between Twilio and OpenAI."""
    print("Client connected")
    await websocket.accept()

    stream_sid = None
    client_audio_path = None
    transcript = ""

    async with websockets.connect(
        'wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01',
        extra_headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "OpenAI-Beta": "realtime=v1"
        }
    ) as openai_ws:
        await initialize_session(openai_ws)

        # Connection specific state
        stream_sid = None
        latest_media_timestamp = 0
        last_assistant_item = None
        mark_queue = []
        response_start_timestamp_twilio = None
        
        async def receive_from_twilio():
            """Receive audio data from Twilio and send it to the OpenAI Realtime API."""
            nonlocal stream_sid, latest_media_timestamp
            try:
                async for message in websocket.iter_text():
                    data = json.loads(message)
                    # print(f"receive_from_twilio event: {data['event']}")
                    if data['event'] == 'media' and openai_ws.open:
                        latest_media_timestamp = int(data['media']['timestamp'])

                        # Write client audio to WAV file
                        if stream_sid and stream_sid in client_audio_files:
                            ulaw_audio = base64.b64decode(data['media']['payload'])

                            pcm_audio = audioop.ulaw2lin(ulaw_audio, 2)  # Convert to 16-bit PCM
                            client_audio_files[stream_sid]["file"].writeframes(pcm_audio)
                            merged_audio_files[stream_sid]["file"].writeframes(pcm_audio)
                            

                        audio_append = {
                            "type": "input_audio_buffer.append",
                            "audio": data['media']['payload']
                        }
                        await openai_ws.send(json.dumps(audio_append))
                    elif data['event'] == 'start':
                        stream_sid = data['start']['streamSid']
                        print(f"Incoming stream has started {stream_sid}")

                        from datetime import datetime

                        # Generate the timestamp
                        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")  # Format: YYYYMMDD_HHMMSS

                        os.makedirs(f"recordings/{client}", exist_ok=True)
                        # Initialize new WAV files for the stream SID
                        client_audio_files[stream_sid] = {
                            "file": initialize_wave_file(f"recordings/{client}/{timestamp}_{stream_sid}_client_audio.wav"),
                            "filename": f"recordings/{client}/{timestamp}_{stream_sid}_client_audio.wav"
                        }
                        bot_audio_files[stream_sid] = {
                            "file": initialize_wave_file(f"recordings/{client}/{timestamp}_{stream_sid}_bot_audio.wav"),
                            "filename": f"recordings/{client}/{timestamp}_{stream_sid}_bot_audio.wav"
                        }
                        merged_audio_files[stream_sid] = {
                            "file": initialize_wave_file(f"recordings/{client}/{timestamp}_{stream_sid}_merged_audio.wav"),
                            "filename": f"recordings/{client}/{timestamp}_{stream_sid}_merged_audio.wav"
                        }

                        response_start_timestamp_twilio = None
                        latest_media_timestamp = 0
                        last_assistant_item = None
                    elif data['event'] == 'mark':
                        if mark_queue:
                            mark_queue.pop(0)
                    elif data['event'] == 'stop':
                        # Close WAV files when client disconnects
                        client_audio_files[stream_sid]["file"].close()
                        bot_audio_files[stream_sid]["file"].close()

                        remove_silence(merged_audio_files[stream_sid]["filename"], merged_audio_files[stream_sid]["filename"].replace('.wav','_trim.wav'))
                        merged_audio_files[stream_sid]["file"].close()

                        del client_audio_files[stream_sid]
                        del bot_audio_files[stream_sid]
                        del merged_audio_files[stream_sid]

            except WebSocketDisconnect:
                print("Client disconnected.")
                if openai_ws.open:
                    await openai_ws.close()



        async def send_to_twilio():
            """Receive events from the OpenAI Realtime API, send audio back to Twilio."""
            nonlocal stream_sid, last_assistant_item, response_start_timestamp_twilio
            try:
                async for openai_message in openai_ws:
                    response = json.loads(openai_message)
                    if response['type'] in LOG_EVENT_TYPES:
                        # print(f"Received event: {response['type']}", response)
                        print(f"Received event: {response['type']}")

                    if response.get('type') == 'response.audio.delta' and 'delta' in response:
                        audio_payload = base64.b64encode(base64.b64decode(response['delta'])).decode('utf-8')

                        # Write bot audio to WAV file
                        if stream_sid and stream_sid in bot_audio_files:
                            ulaw_audio = base64.b64decode(response['delta'])
                            pcm_audio = audioop.ulaw2lin(ulaw_audio, 2)  # Convert to 16-bit PCM
                            bot_audio_files[stream_sid]["file"].writeframes(pcm_audio)
                            merged_audio_files[stream_sid]["file"].writeframes(pcm_audio)

                        audio_delta = {
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {
                                "payload": audio_payload
                            }
                        }
                        await websocket.send_json(audio_delta)

                        if response_start_timestamp_twilio is None:
                            response_start_timestamp_twilio = latest_media_timestamp
                            if SHOW_TIMING_MATH:
                                print(f"Setting start timestamp for new response: {response_start_timestamp_twilio}ms")

                        # Update last_assistant_item safely
                        if response.get('item_id'):
                            last_assistant_item = response['item_id']

                        await send_mark(websocket, stream_sid)

                    # Trigger an interruption. Your use case might work better using `input_audio_buffer.speech_stopped`, or combining the two.
                    if response.get('type') == 'input_audio_buffer.speech_started':
                        print("Speech started detected.")
                        if last_assistant_item:
                            print(f"Interrupting response with id: {last_assistant_item}")
                            await handle_speech_started_event()
                    if response.get('type') == "conversation.item.input_audio_transcription.completed":
                        print("Input Audio Transcription Completed Message")
                        print(f"  Id: {response.get('item_id')}")
                        print(f"  Content Index: {response.get('content_index')}")
                        print(f"  Transcript: {response.get('transcript')}")
            except Exception as e:
                print(f"Error in send_to_twilio: {e}")

        async def handle_speech_started_event():
            """Handle interruption when the caller's speech starts."""
            nonlocal response_start_timestamp_twilio, last_assistant_item
            print("Handling speech started event.")
            if mark_queue and response_start_timestamp_twilio is not None:
                elapsed_time = latest_media_timestamp - response_start_timestamp_twilio
                if SHOW_TIMING_MATH:
                    print(f"Calculating elapsed time for truncation: {latest_media_timestamp} - {response_start_timestamp_twilio} = {elapsed_time}ms")

                if last_assistant_item:
                    if SHOW_TIMING_MATH:
                        print(f"Truncating item with ID: {last_assistant_item}, Truncated at: {elapsed_time}ms")

                    truncate_event = {
                        "type": "conversation.item.truncate",
                        "item_id": last_assistant_item,
                        "content_index": 0,
                        "audio_end_ms": elapsed_time
                    }
                    await openai_ws.send(json.dumps(truncate_event))

                await websocket.send_json({
                    "event": "clear",
                    "streamSid": stream_sid
                })

                mark_queue.clear()
                last_assistant_item = None
                response_start_timestamp_twilio = None

        async def send_mark(connection, stream_sid):
            if stream_sid:
                mark_event = {
                    "event": "mark",
                    "streamSid": stream_sid,
                    "mark": {"name": "responsePart"}
                }
                await connection.send_json(mark_event)
                mark_queue.append('responsePart')

        await asyncio.gather(receive_from_twilio(), send_to_twilio())

async def send_initial_conversation_item(openai_ws):
    """Send initial conversation item if AI talks first."""
    initial_conversation_item = {
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": SYSTEM_MESSAGE
                }
            ]
        }
    }
    await openai_ws.send(json.dumps(initial_conversation_item))
    await openai_ws.send(json.dumps({"type": "response.create"}))


async def initialize_session(openai_ws):
    """Control initial session with OpenAI."""
    session_update = {
        "type": "session.update",
        "session": {
            "turn_detection": {
                "type": "server_vad",
                "threshold": 0.6,
                "prefix_padding_ms": 300,
                "silence_duration_ms": 500
            },            
            "input_audio_format": "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "voice": VOICE,
            "instructions": SYSTEM_MESSAGE,
            "modalities": ["text", "audio"],
            "temperature": 0.6,
            "input_audio_transcription": {
            "model": "whisper-1"
        },
        }
    }
    # print('Sending session update:', json.dumps(session_update))
    await openai_ws.send(json.dumps(session_update))
    await send_initial_conversation_item(openai_ws)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port="8082")
