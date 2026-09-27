"""Evaluation-only formatting clarification. Production prompt is unchanged."""

FORMAT_CLARIFICATION = '''
Output format clarification (classification rules above are unchanged):
Return ONE valid JSON object, not a list of field names, CSV, YAML or Markdown.
Use double-quoted JSON keys and strings. Do not add text outside the JSON object.
The top-level keys must be exactly "assessment", "explanation", "findings".
"assessment" is one of "observed_fall", "suspected_fall", "normal_activity", "unobservable".
"explanation" is a Korean string of visible evidence, no more than 1000 characters.
"findings" is a JSON array, with at most 8 objects, one per concerning person.
Each finding has exactly "assessment", "kind", "regions".
A finding's assessment is "observed_fall" or "suspected_fall".
"kind" is "motion_seen", "already_down", or "unknown".
"regions" is [] if reliable locations are unavailable; otherwise it contains
2 to 4 objects, each with exactly "frame_index" and "box".
"frame_index" is an integer from 0 through the supplied image count minus 1.
Indices within a finding must be distinct and increasing.
"box" MUST be an array of FOUR NUMBERS: [left, top, right, bottom].
Do NOT return a coordinate object such as {"left":...,"top":...}.
Every coordinate is normalized to 0 through 1, NOT pixel coordinates or percentages.
Require left < right and top < bottom. For example [0.1, 0.2, 0.6, 0.8]
demonstrates ONLY the format; determine actual coordinates from the images.
An observed_fall finding must have kind motion_seen. Scene assessment must agree
with findings as specified above. For normal_activity or unobservable use findings: [].
Do not change a judgment or omit a concerning person merely to avoid giving locations.
'''

# Additive experiment: never edit FORMAT_CLARIFICATION or the production prompt
# to revise a completed experiment. No post-response coordinate repair is added.
NORMALIZED_BOX_CHECKLIST = '''
Coordinate output checklist (video classification rules are unchanged):
Use the full supplied image as the coordinate reference, not a crop or a model's
internal resized image. The origin is the top-left corner; x increases to the
right and y increases downward. The bottom-right corner is (1.0, 1.0).
Every box has exactly these FOUR values in this order:
  box[0] = left:   horizontal position of the left edge / supplied image width.
  box[1] = top:    vertical position of the top edge / supplied image height.
  box[2] = right:  horizontal position of the right edge / supplied image width.
  box[3] = bottom: vertical position of the bottom edge / supplied image height.
ALL FOUR values use the SAME 0.0 to 1.0 scale. Normalize BOTH axes, not just x.
Do not output pixels, percentages, 0-to-1000 coordinates, or top-left-bottom-right
order. Do not mix coordinate scales between values, boxes, or frames.
Format-only example: {"frame_index": 0, "box": [0.1, 0.2, 0.6, 0.8]}.
Do not copy example coordinates; locate the visible person in each selected frame.
Before returning JSON, check EACH box silently:
  0.0 <= left < right <= 1.0 AND 0.0 <= top < bottom <= 1.0.
Also check that the box encloses the SAME concerning person at its frame_index,
not furniture, bedding, or another person. Do not invent hidden body parts.
If these checks fail, reconsider the location from that image. Do not merely
clamp numbers or swap edges to manufacture a valid-looking box.
If reliable locations remain unavailable, keep that person's assessment and kind
with regions: []. Otherwise provide 2 to 4 distinct, increasing frame indices.
Do not remove a concerning finding, change its assessment, or output empty
regions solely to pass the format checks. Never invent a box to avoid regions: [].
Output only the specified JSON object, without this checklist or extra fields.
'''

PROMPT_ADDITIONS = {
    'runtime_crosscheck_with_findings': '',
    'explicit_json_v2': FORMAT_CLARIFICATION,
    'normalized_boxes_v3': FORMAT_CLARIFICATION + NORMALIZED_BOX_CHECKLIST,
    'native_boxes_v4': '',  # Replaces conflicting coordinate clauses; see below.
}


def native_box_prompt(runtime_prompt):
    """Evaluation-only wire contract from Google's documented Gemma box format.

    Keep classification text identical to v2. Do not append a contradictory unit
    override, and do not change the completed v2/v3 experiments.
    https://ai.google.dev/gemma/docs/capabilities/vision/image
    """
    text = runtime_prompt + FORMAT_CLARIFICATION
    replacements = (
        ('and box (normalized left,top,right,bottom).',
         'and box_2d (integer top,left,bottom,right on a 0-to-1000 grid).'),
        ('each with exactly "frame_index" and "box".',
         'each with exactly "frame_index" and "box_2d".'),
        ('"box" MUST be an array of FOUR NUMBERS: [left, top, right, bottom].\n'
         'Do NOT return a coordinate object such as {"left":...,"top":...}.\n'
         'Every coordinate is normalized to 0 through 1, NOT pixel coordinates or percentages.\n'
         'Require left < right and top < bottom. For example [0.1, 0.2, 0.6, 0.8]\n'
         'demonstrates ONLY the format; determine actual coordinates from the images.',
         '"box_2d" MUST be an array of FOUR INTEGERS: [ymin, xmin, ymax, xmax],\n'
         'meaning [top, left, bottom, right]. Normalize ALL FOUR coordinates to\n'
         'the same 0-to-1000 grid relative to the FULL supplied image.\n'
         'The top-left is (0, 0); the bottom-right is (1000, 1000).\n'
         'Require 0 <= ymin < ymax <= 1000 and 0 <= xmin < xmax <= 1000.\n'
         'Do not output fractions, pixel coordinates, percentages, or coordinate objects.\n'
         'For example [200, 100, 800, 600] demonstrates ONLY the format; determine\n'
         'actual coordinates from the images. Keep regions: [] if locations are unreliable.'),
    )
    for old, new in replacements:
        if text.count(old) != 1:
            raise ValueError('native prompt source changed: review coordinate clauses')
        text = text.replace(old, new)
    return text
