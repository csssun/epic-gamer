"""Adapt the solver's Google-shaped inputs to New API Chat Completions.

Only the solver modules' client factories are replaced. The Google SDK is not
patched globally; no Google upload or generateContent endpoint is contacted.
"""
import base64
import importlib
import inspect
import json
import mimetypes
import re
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
from google.genai import types
from pydantic import BaseModel


class NewAPIError(RuntimeError):
    pass


def completion_url(base_url):
    url = base_url.strip().rstrip('/')
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        raise NewAPIError('NEW_API_BASE_URL must be an absolute HTTP(S) URL')
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise NewAPIError('NEW_API_BASE_URL must not contain credentials, query, or fragment')
    if url.endswith('/chat/completions'):
        return url
    if not url.endswith('/v1'):
        url += '/v1'
    return url + '/chat/completions'


def strip_fence(text):
    text = text.strip()
    match = re.fullmatch(r'```(?:json)?\s*(.*?)\s*```', text, re.S | re.I)
    return match[1].strip() if match else text


class LocalImages:
    async def upload(self, *, file, **kwargs):
        if isinstance(file, (str, Path)):
            path = Path(file)
            data = path.read_bytes()
            mime = mimetypes.guess_type(path.name)[0] or 'image/png'
        else:
            data = file.read() if hasattr(file, 'read') else bytes(file)
            if inspect.isawaitable(data):
                data = await data
            mime = 'image/png'
        if not mime.startswith('image/'):
            raise NewAPIError('Only image inputs are supported by this adapter')
        uri = f'data:{mime};base64,' + base64.b64encode(data).decode('ascii')
        return types.File(name='local-image', uri=uri, mime_type=mime)


def content_parts(content):
    if isinstance(content, str):
        return [{'type': 'text', 'text': content}]
    if isinstance(content, dict):
        content = types.Content.model_validate(content)
    parts = content.parts if isinstance(content, types.Content) else [content]
    result = []
    for part in parts or []:
        if isinstance(part, str):
            result.append({'type': 'text', 'text': part})
        elif part.text is not None:
            result.append({'type': 'text', 'text': part.text})
        elif part.inline_data is not None:
            blob = part.inline_data
            if not blob.mime_type.startswith('image/'):
                raise NewAPIError('Unsupported inline media type')
            uri = f'data:{blob.mime_type};base64,' + base64.b64encode(blob.data).decode()
            result.append({'type': 'image_url', 'image_url': {'url': uri}})
        elif part.file_data is not None:
            uri = part.file_data.file_uri
            if not uri.startswith('data:image/'):
                raise NewAPIError('Expected an embedded image; remote Google file URLs are unsupported')
            result.append({'type': 'image_url', 'image_url': {'url': uri}})
        else:
            raise NewAPIError('Unsupported model content part')
    return result


class NewAPIClient:
    def __init__(self, settings, *, transport=None):
        self.settings = settings
        self.url = completion_url(settings.NEW_API_BASE_URL)
        self.key = settings.NEW_API_API_KEY.get_secret_value()
        if not self.key or not settings.NEW_API_MODEL.strip():
            raise NewAPIError('NEW_API_API_KEY and NEW_API_MODEL are required')
        self.transport = transport
        self.aio = SimpleNamespace(files=LocalImages(), models=self)

    def payload(self, contents, config):
        config = config or types.GenerateContentConfig()
        if isinstance(config, dict):
            config = types.GenerateContentConfig(**config)
        messages = []
        if config.system_instruction:
            parts = content_parts(config.system_instruction)
            messages.append({'role': 'system', 'content': parts})
        if not isinstance(contents, list):
            contents = [contents]
        for content in contents:
            role = getattr(content, 'role', None) or (content.get('role') if isinstance(content, dict) else None)
            messages.append({'role': 'assistant' if role == 'model' else 'user',
                             'content': content_parts(content)})
        schema = config.response_schema
        enum_type = isinstance(schema, type) and issubclass(schema, Enum)
        schema_dict = None
        if isinstance(schema, type) and issubclass(schema, BaseModel):
            schema_dict = schema.model_json_schema()
        elif isinstance(schema, dict):
            schema_dict = schema
        payload = {'model': self.settings.NEW_API_MODEL, 'messages': messages, 'stream': False}
        if enum_type:
            choices = [e.value for e in schema]
            messages.insert(0, {'role': 'system', 'content':
                'Return exactly one of these values, with no quotes or explanation: ' + json.dumps(choices)})
        elif schema_dict:
            messages.insert(0, {'role': 'system', 'content':
                'Return only a JSON object conforming to this JSON Schema: ' + json.dumps(schema_dict)})
            mode = self.settings.NEW_API_RESPONSE_FORMAT
            if mode == 'json_object':
                payload['response_format'] = {'type': 'json_object'}
            elif mode == 'json_schema':
                payload['response_format'] = {'type': 'json_schema', 'json_schema': {
                    'name': 'challenge_result', 'schema': schema_dict,
                }}
        return payload, schema

    async def generate_content(self, *, model=None, contents, config=None, **kwargs):
        payload, schema = self.payload(contents, config)
        async with httpx.AsyncClient(timeout=self.settings.NEW_API_TIMEOUT,
                                     transport=self.transport) as client:
            try:
                response = await client.post(self.url, headers={
                    'Authorization': 'Bearer ' + self.key,
                    'Content-Type': 'application/json',
                }, json=payload)
            except httpx.HTTPError as exc:
                # Do not include request headers, response bodies, or credentials.
                raise NewAPIError(f'New API network request failed: {type(exc).__name__}') from None
        if response.status_code != 200:
            raise NewAPIError(f'New API returned HTTP {response.status_code}; '
                              'check gateway address, token, model/channel and response format')
        try:
            choice = response.json()['choices'][0]
            if choice.get('finish_reason') in ('length', 'content_filter'):
                raise NewAPIError('Model output was truncated or filtered')
            text = choice['message']['content']
            if not isinstance(text, str) or not text.strip():
                raise NewAPIError('New API returned empty/non-text model output')
        except (KeyError, IndexError, TypeError, ValueError):
            raise NewAPIError('New API response is not a valid Chat Completions response') from None
        text = strip_fence(text)
        parsed = None
        if isinstance(schema, type) and issubclass(schema, Enum):
            text = text.strip('"')
            parsed = schema(text)
        elif isinstance(schema, type) and issubclass(schema, BaseModel):
            try:
                parsed = schema.model_validate_json(text)
            except ValueError:
                raise NewAPIError('Model returned JSON that does not match the required schema') from None
        return types.GenerateContentResponse(candidates=[types.Candidate(
            content=types.Content(role='model', parts=[types.Part.from_text(text=text)]),
            finish_reason='STOP',
        )], parsed=parsed)


def install_new_api_adapter(settings, *, transport=None):
    # Version pinned in uv.lock. Scope the factory replacement to the five
    # solver modules using it, rather than monkeypatching google.genai.Client.
    modules = ['challenge_classifier', 'image_classifier', 'spatial_point_reasoning',
               'spatial_path_reasoning', 'spatial_bbox_reasoning']
    def factory(*args, **kwargs):
        return NewAPIClient(settings, transport=transport)
    for name in modules:
        module = importlib.import_module('hcaptcha_challenger.tools.' + name)
        module.genai = SimpleNamespace(Client=factory)
