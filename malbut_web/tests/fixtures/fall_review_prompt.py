"""Cross-language fixture: the robot's incident prompt, payload and reply parsing.

The web AI review must send what the robot sends for an incident without a
target box, and accept/reject the same replies. No HTTP or model call.
"""

import io
import json
import sys

from malbut_agent_server.adapters.outbound import ollama_cloud_fall as o
from malbut_agent_server.domain.fall_monitoring import CloudFallRequest, FrameWindow, RgbFrame
from malbut_agent_server.ports.cloud_fall import CloudFallProviderError


def jpeg():
    # Pillow is optional here (the web CI image has no Pillow); the payload
    # check needs it, the prompt and reply checks do not.
    from PIL import Image
    out = io.BytesIO()
    Image.new('RGB', (640, 400), (30, 60, 90)).save(out, format='JPEG')
    return out.getvalue()


offsets = json.loads(sys.argv[1])
try:
    frames = tuple(RgbFrame(100.0 + offset, jpeg()) for offset in offsets)
except ImportError:
    frames = (RgbFrame(100.0, b'\xff\xd8test\xff\xd9'),)
    payload = None
else:
    payload = 'pending'
request = CloudFallRequest(
    request_id='r1', purpose='incident', device_id='robot-a', boot_id='boot-1',
    incident_id='11111111-1111-4111-8111-111111111111', subject_key=None, evidence_revision=1,
    window=FrameWindow(frames, 100.0, 105.0, False), sensors=None, target=None)
if payload is not None:
    payload = json.loads(o.build_payload(request, model='gemma4:31b', box_format=o.NATIVE_BOX_FORMAT))
    for message in payload['messages']:
        message.pop('images', None)

replies = {}
for name, body in json.loads(sys.argv[2]).items():
    try:
        reply = o.parse_reply(body.encode(), request, box_format=o.NATIVE_BOX_FORMAT)
        replies[name] = dict(assessment=reply.assessment.value, explanation=reply.explanation)
    except CloudFallProviderError as error:
        replies[name] = str(error)

print(json.dumps(dict(system=o.SYSTEM_PROMPT, userPrefix=o.USER_PREFIX, payload=payload,
                      replies=replies), ensure_ascii=False))
