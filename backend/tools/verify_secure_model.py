"""User-side transport check. Defaults to no outbound call; never prints secrets.

Run from backend/: python tools/verify_secure_model.py
Explicit two-generated-image probe: add --probe --acknowledge-upload.
Loopback tunnels additionally require --authenticated-loopback-tunnel.
"""
from __future__ import annotations
import argparse
from io import BytesIO
import json
from pathlib import Path
import sys
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings
from app.services.vision.authorization import authorize_upload
from app.services.vision.errors import ModelError
from app.services.vision.providers.base import ImagePart, VisionRequest
from app.services.vision.providers.openai_compatible import OpenAICompatibleVisionProvider, validate_endpoint


def check_config(*, authenticated_loopback_tunnel=False):
    settings = get_settings()
    validate_endpoint(settings.model_base_url, allow_insecure=False)
    if settings.model_allow_insecure_http:
        raise ModelError('INSECURE_DEVELOPMENT_EXCEPTION', 'disable insecure HTTP before acceptance')
    if urlparse(settings.model_base_url).scheme != 'https' and not authenticated_loopback_tunnel:
        raise ModelError('TUNNEL_CONFIRMATION_REQUIRED', 'local HTTP requires an independently authenticated encrypted tunnel')
    if not settings.model_api_key:
        raise ModelError('MODEL_NOT_CONFIGURED', 'model credential is missing')
    return {'configuration_checked': True, 'outbound_calls': 0,
            'transport': 'https' if urlparse(settings.model_base_url).scheme == 'https' else 'user-confirmed-tunnel',
            'mode': settings.model_mode, 'insecure_development_exception': False,
            'real_footage_validated': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--probe', action='store_true')
    parser.add_argument('--acknowledge-upload', action='store_true')
    parser.add_argument('--authenticated-loopback-tunnel', action='store_true')
    args = parser.parse_args()
    try:
        report = check_config(authenticated_loopback_tunnel=args.authenticated_loopback_tunnel)
        if args.probe:
            context = authorize_upload(project_id='secure-transport-probe',
                upload_acknowledged=args.acknowledge_upload, purpose='two-generated-image-transport-check')
            from PIL import Image
            images = []
            for color in ('red', 'blue'):
                memory = BytesIO(); Image.new('RGB', (32, 32), color).save(memory, format='PNG')
                images.append(ImagePart(data=memory.getvalue(), mime='image/png', label=color))
            response = OpenAICompatibleVisionProvider().complete(VisionRequest(
                prompt='Return one JSON object with image_count and the colors of these two solid images.',
                images=images, json_object=True, authorization=context))
            payload = json.loads(response.text)
            if not isinstance(payload, dict) or payload.get('image_count') != 2:
                raise ModelError('MODEL_INVALID_OUTPUT', 'two-image probe was not confirmed')
            report.update({'outbound_calls': response.attempts, 'images_in_probe': 2,
                           'structured_response': True, 'usage': response.usage})
        print(json.dumps(report, ensure_ascii=False))
        return 0
    except (ModelError, ValueError) as exc:
        print(json.dumps({'configuration_checked': False,
                          'error_code': getattr(exc, 'code', 'MODEL_INVALID_OUTPUT'),
                          'real_footage_validated': False}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
