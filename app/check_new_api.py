"""One small multimodal request to validate New API configuration before login."""
import asyncio
import io
from google.genai import types
from PIL import Image
from pydantic import BaseModel
from services.new_api_adapter import NewAPIClient
from settings import settings


class ProbeResult(BaseModel):
    color: str


async def main():
    data = io.BytesIO()
    Image.new('RGB', (64, 64), (0, 255, 0)).save(data, format='PNG')
    response = await NewAPIClient(settings).generate_content(contents=[types.Content(
        role='user', parts=[types.Part.from_bytes(data=data.getvalue(), mime_type='image/png'),
                           types.Part.from_text(text='Identify the solid image color. Use English.')],
    )], config=types.GenerateContentConfig(response_schema=ProbeResult))
    if response.parsed.color.strip().lower() != 'green':
        raise RuntimeError('Vision probe failed: model did not identify the green image')
    print('NEW_API_CHECK_OK: image input and structured response verified')


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as exc:
        print(f'NEW_API_CHECK_FAILED: {type(exc).__name__}: {exc}')
        raise SystemExit(1)
