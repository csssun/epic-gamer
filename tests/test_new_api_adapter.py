import asyncio
import json
from enum import Enum

import httpx
import pytest
from google.genai import types
from PIL import Image
from pydantic import BaseModel

from services.new_api_adapter import (NewAPIClient, NewAPIError, completion_url,
                                      install_new_api_adapter)
from settings import EpicSettings


class Answer(BaseModel):
    answer: int


class Label(str, Enum):
    YES = 'yes'
    NO = 'no'


def config(**kwargs):
    return EpicSettings(_env_file=None, NEW_API_BASE_URL='https://gateway.example/v1',
                        NEW_API_API_KEY='dummy-test-key', NEW_API_MODEL='my-vision-alias', **kwargs)


@pytest.mark.parametrize('base', ['https://gateway.example', 'https://gateway.example/',
                                 'https://gateway.example/v1', 'https://gateway.example/v1/',
                                 'https://gateway.example/v1/chat/completions'])
def test_base_url_normalization(base):
    assert completion_url(base) == 'https://gateway.example/v1/chat/completions'


@pytest.mark.parametrize('base', ['', 'gateway.example', 'https://u:p@gateway.example',
                                 'https://gateway.example?key=secret'])
def test_invalid_base_url(base):
    with pytest.raises(NewAPIError):
        completion_url(base)


@pytest.mark.parametrize('mode', ['prompt', 'json_object', 'json_schema'])
def test_multimodal_request_response_roundtrip(tmp_path, mode):
    image = tmp_path / 'image.png'
    Image.new('RGB', (64, 64), 'green').save(image)
    seen = []
    def handler(request):
        seen.append(request)
        assert str(request.url) == 'https://gateway.example/v1/chat/completions'
        assert request.headers['Authorization'] == 'Bearer dummy-test-key'
        body = json.loads(request.content)
        assert body['model'] == 'my-vision-alias'
        if mode == 'prompt':
            assert 'response_format' not in body
        else:
            assert body['response_format']['type'] == mode
        parts = [p for m in body['messages'] if isinstance(m['content'], list) for p in m['content']]
        assert any(p.get('image_url', {}).get('url', '').startswith('data:image/png;base64,') for p in parts)
        assert 'generationConfig' not in body and 'contents' not in body
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {
            'content': '```json\n{"answer":42}\n```'}}]})
    async def run():
        client = NewAPIClient(config(NEW_API_RESPONSE_FORMAT=mode), transport=httpx.MockTransport(handler))
        file = await client.aio.files.upload(file=image)
        return await client.aio.models.generate_content(
            model='ignored-google-model', contents=[types.Content(role='user', parts=[
                types.Part.from_uri(file_uri=file.uri, mime_type=file.mime_type),
                types.Part.from_text(text='Identify the image'),
            ])], config=types.GenerateContentConfig(response_schema=Answer, system_instruction='Be precise'))
    response = asyncio.run(run())
    assert response.parsed.answer == 42
    assert response.text == '{"answer":42}'
    assert response.model_dump(mode='json')['parsed'] == {'answer': 42}
    assert len(seen) == 1  # File preparation never calls any upload endpoint.


def test_inline_image_and_enum_response():
    def handler(request):
        body = json.loads(request.content)
        assert 'response_format' not in body
        return httpx.Response(200, json={'choices': [{'message': {'content': 'yes'}}]})
    async def run():
        client = NewAPIClient(config(), transport=httpx.MockTransport(handler))
        return await client.generate_content(contents=[types.Content(role='user', parts=[
            types.Part.from_bytes(data=b'test-image', mime_type='image/png')])],
            config=types.GenerateContentConfig(response_schema=Label))
    assert asyncio.run(run()).parsed == Label.YES


@pytest.mark.parametrize('status', [400, 401, 403, 429, 500])
def test_http_errors_do_not_expose_token_or_response_body(status):
    transport = httpx.MockTransport(lambda request: httpx.Response(status, text='dummy-test-key'))
    with pytest.raises(NewAPIError) as error:
        asyncio.run(NewAPIClient(config(), transport=transport).generate_content(contents='hello'))
    assert str(status) in str(error.value)
    assert 'dummy-test-key' not in str(error.value)


def test_bad_json_is_rejected():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        'choices': [{'message': {'content': '{"wrong":1}'}}]}))
    with pytest.raises(NewAPIError, match='schema'):
        asyncio.run(NewAPIClient(config(), transport=transport).generate_content(
            contents='hello', config=types.GenerateContentConfig(response_schema=Answer)))


def test_real_solver_library_uses_new_api(tmp_path):
    from google import genai
    original_google_client = genai.Client
    image = tmp_path / 'image.png'
    Image.new('RGB', (64, 64), 'green').save(image)
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps({
            'challenge_prompt': 'fixture only', 'coordinates': [{'box_2d': [0, 0]}],
        })}}]})
    install_new_api_adapter(config(), transport=httpx.MockTransport(handler))
    from hcaptcha_challenger.tools.image_classifier import ImageClassifier
    result = asyncio.run(ImageClassifier(gemini_api_key='internal-placeholder',
                                        constraint_response_schema=True).invoke_async(image))
    assert result.coordinates[0].box_2d == [0, 0]
    assert calls[0]['model'] == 'my-vision-alias'
    assert genai.Client is original_google_client
