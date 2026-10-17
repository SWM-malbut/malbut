// Generated from malbut_interfaces/contracts/fall_events.json by
// scripts/sync-fall-contract.mjs. Do not edit; change the source and run it again.
export const FALL_CONTRACT = {
  "schemaVersion": 1,
  "notes": {
    "about": "Robot-to-web fall incident event contract. The robot journal writes these values and the web accepts only these. Change this file, then run `node scripts/sync-fall-contract.mjs` in malbut_web; CI fails while the web copy differs.",
    "eventKinds": "approach_*/person_check_completed: the robot drives near an uncertain suspicion and checks for a person first. view_completed/posture_check_completed: only part of the body seen, it moves to see the whole body, then checks the posture. check_waiting/check_not_confirmed/revisit_started/same_person_compared: could not ask yet (manual drive or another job), a minute of that, going back to the spot before asking, and comparing the person with the first scene (mockup 28). check_stopped: 'stop' during the check, stopped without a question (2026-10-13). arrival_compared/reverify_completed/departure_reviewed/fall_moment_reviewed/recheck_started: the one check for every suspicion (SWM25-261, mockups 29-31): compare the first sight with the arrival, look again from where it was first seen, what left the spot, whether the fall moment was a fall, and the robot checking a closed or left-open incident again (a new round).",
    "reasonsByKind": "Allowed reasons per event kind; null means no reason (only an approach start that interrupted nothing). Kinds not listed accept any reason matching ^[a-z_]{1,80}$. person_check_completed nobody_there: went back after waiting and found no one, not a closure. posture_check_completed: off the floor only when the robot's measurement and Cloud agree, anything else asks; active_on_floor is sitting up busy on the floor, seen twice, no fall seen (2026-10-14). same_person_compared: clothing only, for reference, never changes what the robot does. recheck_started: stood_up, floor_recheck (30 minutes after a close on the floor), person_seen_again (someone at a spot left open with nobody there), agent_restart.",
    "resolvedReasons": "A robot-side closure after its own check, never a normal-activity judgment. guardian_closed: a member pressed 처리 완료 and the robot stopped trying. left_the_spot: a person was seen walking away from the spot.",
    "notificationReasons": "Reason -> notification level. Web-only reasons are never accepted from a robot. Per-reason ones are sent once per reason per incident, outside the 'only a higher level' rule. checkRound: every event may carry the robot's check round (1 when absent); recheck_started starts the next. Notifications are counted per round, so a recheck can alert again (2026-10-17).",
    "cloudCriteria": "Shared judgment criteria for every Cloud request, on the robot and in the web AI review (the user, 2026-10-17): the same three questions instead of named objects. Each request adds only its own question and answer format. cloudAnswers lists the answer values of the robot's Cloud looks."
  },
  "states": [
    "verifying",
    "recheck_required",
    "help_required",
    "resolved"
  ],
  "assessments": [
    "observed_fall",
    "suspected_fall",
    "normal_activity",
    "unobservable"
  ],
  "answers": [
    "help_request",
    "okay",
    "unclear",
    "no_response",
    "failed"
  ],
  "eventKinds": [
    "incident_opened",
    "incident_updated",
    "question_requested",
    "voice_result",
    "decision_required",
    "notification_requested",
    "agent_check_failed",
    "analysis_completed",
    "analysis_unavailable",
    "stale_analysis_result",
    "recheck_unavailable",
    "incident_resolved",
    "confirmation_completed",
    "incident_merged",
    "approach_started",
    "approach_completed",
    "person_check_completed",
    "approach_returned",
    "view_completed",
    "posture_check_completed",
    "check_waiting",
    "check_not_confirmed",
    "revisit_started",
    "same_person_compared",
    "check_stopped",
    "arrival_compared",
    "reverify_completed",
    "departure_reviewed",
    "fall_moment_reviewed",
    "recheck_started"
  ],
  "reasonsByKind": {
    "approach_started": [
      "patrol_stopped",
      "follow_stopped",
      null
    ],
    "approach_completed": [
      "arrived",
      "no_map",
      "no_path",
      "timeout",
      "failed",
      "rejected"
    ],
    "person_check_completed": [
      "person",
      "not_a_person",
      "partial_body",
      "whole_body",
      "nobody_there"
    ],
    "approach_returned": [
      "returned",
      "return_failed"
    ],
    "view_completed": [
      "arrived",
      "no_map",
      "no_path",
      "timeout",
      "failed",
      "rejected"
    ],
    "posture_check_completed": [
      "sitting_off_floor",
      "standing",
      "active_on_floor",
      "on_floor",
      "body_unseen",
      "judgements_differ",
      "posture_unconfirmed"
    ],
    "check_waiting": [
      "manual_drive",
      "busy"
    ],
    "check_not_confirmed": [
      "waited_manual_drive",
      "waited_busy"
    ],
    "revisit_started": [
      "manual_drive",
      "busy"
    ],
    "check_stopped": [
      "user_stop"
    ],
    "same_person_compared": [
      "same",
      "different",
      "unclear"
    ],
    "confirmation_completed": [
      "confirmed_incident",
      "resolved",
      "unknown"
    ],
    "arrival_compared": [
      "same_person",
      "person_unsure",
      "different_person",
      "same_object",
      "gone",
      "not_visible",
      "failed"
    ],
    "reverify_completed": [
      "arrived",
      "no_map",
      "no_path",
      "timeout",
      "failed",
      "rejected"
    ],
    "departure_reviewed": [
      "animal_left",
      "person_walked",
      "person_crawled",
      "not_seen",
      "failed"
    ],
    "fall_moment_reviewed": [
      "fall",
      "self",
      "unknown"
    ],
    "recheck_started": [
      "stood_up",
      "floor_recheck",
      "person_seen_again",
      "agent_restart"
    ]
  },
  "resolvedReasons": [
    "normal_verified",
    "risk_cleared",
    "response_completed",
    "not_a_person",
    "off_floor_verified",
    "floor_activity_verified",
    "guardian_closed",
    "left_the_spot"
  ],
  "analysisPurposes": {
    "analysis_completed": [
      "incident",
      "crosscheck"
    ],
    "person_check_completed": [
      "person_check"
    ],
    "posture_check_completed": [
      "posture_check"
    ],
    "same_person_compared": [
      "same_person_check"
    ],
    "arrival_compared": [
      "arrival_check"
    ],
    "departure_reviewed": [
      "departure_check"
    ]
  },
  "analysisExplanationMaxChars": 1000,
  "notificationLevels": [
    "info",
    "check",
    "urgent"
  ],
  "notificationReasons": {
    "fall_observed_person_okay": "info",
    "person_no_response": "check",
    "check_required_not_confirmed_fall": "check",
    "help_requested": "urgent",
    "confirmation_help_required": "urgent",
    "robot_could_not_confirm": "urgent",
    "revisit_same_person_off_floor": "info",
    "revisit_person_unclear_off_floor": "check",
    "revisit_nobody_there": "check",
    "scene_answer_unconfirmed": "check",
    "check_stopped_by_user": "check",
    "reopened_by_opinion": "check",
    "person_may_differ": "check",
    "left_unseen": "check",
    "could_not_compare": "check",
    "person_crawled_away": "check",
    "closed_after_alert": "info"
  },
  "webOnlyNotificationReasons": [
    "reopened_by_opinion"
  ],
  "perReasonNotificationReasons": [
    "robot_could_not_confirm",
    "revisit_same_person_off_floor",
    "revisit_person_unclear_off_floor",
    "revisit_nobody_there",
    "scene_answer_unconfirmed",
    "check_stopped_by_user",
    "person_may_differ",
    "left_unseen",
    "could_not_compare",
    "person_crawled_away",
    "closed_after_alert"
  ],
  "cloudCriteria": "Judge every possible person with the same three questions, in this order, and never by the kind of object:\n1. Is a human body itself directly visible? Count a person only when actual body parts that belong together (head, face, hair, arm, hand, leg, foot or torso) are visible. Something that only has the size, colour, outline or position of a person is not a person.\n2. Is that body physically in the room in front of the camera? A flat surface that shows an image (a reflection, a picture or a display) shows the room or another scene; it is not a space behind that surface. A body seen only inside such an image is not a person in the room, even if it looks fallen and is the only person visible. A body directly visible in the room is judged as usual.\n3. Is the posture ordinary rest or ordinary activity? Lying or sitting on a raised surface such as a bed, sofa or chair is ordinary. Controlled sitting or lying down is ordinary: lowering slowly, using the hands, knees or furniture for support, or folding the legs. A person already sitting upright on the floor who moves purposefully (using the hands, changing posture or handling objects) is ordinary even if the descent was not seen. A sudden drop, legs giving way, falling backward or sideways, or a hard landing is a fall even if the person ends up sitting. Lying still on the floor, slumping or struggling to get up is not ordinary.",
  "cloudAnswers": {
    "posture": [
      "furniture",
      "standing",
      "floor_active",
      "floor",
      "unclear"
    ],
    "earlier": [
      "fall",
      "self",
      "unknown"
    ],
    "arrival": [
      "same_person",
      "person_unsure",
      "different_person",
      "same_object",
      "gone",
      "not_visible"
    ],
    "departure": [
      "animal_left",
      "person_walked",
      "person_crawled",
      "not_seen"
    ]
  }
};
