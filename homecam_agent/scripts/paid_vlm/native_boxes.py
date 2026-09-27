"""Explicit evaluation-only 1000-grid yxyx -> unit xyxy adapter; no guessing."""

import json
import re

from .providers import strict_json

PROFILE = 'native_boxes_v4'
WIRE_FORMAT = 'box_2d_yxyx_int_0_1000'


class NativeBoxError(ValueError):
    pass


def check(condition, code):
    if not condition:
        raise NativeBoxError(code)


def normalize_native_reply(text):
    """Validate every native box before publishing any normalized response.

    Reject bools/floats, mixed units, alternate keys, out-of-range and reversed
    bounds. Never clamp, sort, infer a scale, extract JSON or retain only good
    regions. The original response string is not modified.
    """
    check(isinstance(text, str) and len(text) <= 12000, 'response_contract_error')
    content = text.strip()
    fence = re.fullmatch(r'```(?:json)?\s*\n(.*?)\n```', content, re.DOTALL)
    if fence:
        content = fence.group(1)
    try:
        obj = strict_json(content)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise NativeBoxError('invalid_json') from None
    check(isinstance(obj, dict) and set(obj) == {'assessment', 'explanation', 'findings'},
          'response_contract_error')
    check(isinstance(obj['findings'], list), 'response_contract_error')
    for finding in obj['findings']:
        check(isinstance(finding, dict) and set(finding) == {'assessment', 'kind', 'regions'}
              and isinstance(finding['regions'], list), 'response_contract_error')
        for region in finding['regions']:
            check(isinstance(region, dict) and set(region) == {'frame_index', 'box_2d'},
                  'invalid_native_region_fields')
            box = region['box_2d']
            check(isinstance(box, list) and len(box) == 4, 'invalid_box_shape')
            check(all(type(v) is int for v in box), 'invalid_native_box_type')
            check(all(0 <= v <= 1000 for v in box), 'invalid_box_range')
            top, left, bottom, right = box
            check(top < bottom and left < right, 'invalid_box_extent')
            del region['box_2d']
            region['box'] = [left / 1000, top / 1000, right / 1000, bottom / 1000]
    # Existing strict validator remains responsible for labels, scene/finding
    # consistency, indices, counts and explanation constraints after conversion.
    return json.dumps(obj, ensure_ascii=False, allow_nan=False)
